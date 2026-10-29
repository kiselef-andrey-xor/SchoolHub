"""Провижининг и синхронизация доменных пользователей Active Directory.

Правила (согласно выбранной модели доступа):
  * вход разрешён **любому** пользователю домена (или только членам групп из
    `LDAP_ALLOWED_GROUPS`, если список задан);
  * при первом входе учётка создаётся автоматически с ролью `LDAP_DEFAULT_ROLE`
    (по умолчанию — Учитель);
  * роли раздаёт Владелец в интерфейсе. Группы AD могут только **повышать** роль
    (маппинг `LDAP_ADMIN_GROUPS` / `LDAP_OWNER_GROUPS`), но никогда не понижают её
    на входе — иначе исключение из группы молча лишило бы человека доступа;
  * пароль домена не хранится: `password_hash = UNUSABLE_PASSWORD`;
  * ФИО, должность, телефон и e-mail подтягиваются из AD при каждом входе
    (отключается `LDAP_SYNC_ON_LOGIN=false`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import ROLE_LEVEL, Role
from app.errors import forbidden
from app.models.user import UNUSABLE_PASSWORD, User
from app.services import ad, audit
from app.services.ad import ADPrincipal, candidate_emails, normalize_username

logger = logging.getLogger("schoolhub.ad")

AUTH_LDAP = "ldap"
AUTH_LOCAL = "local"


@dataclass(slots=True)
class ProvisionResult:
    user: User
    created: bool
    role_changed: bool = False


def _display_name(principal: ADPrincipal) -> str:
    if principal.full_name:
        return principal.full_name[:255]
    parts = [principal.last_name, principal.first_name]
    joined = " ".join(p for p in parts if p).strip()
    return (joined or principal.username or "Пользователь домена")[:255]


def _email(principal: ADPrincipal) -> str:
    email = (principal.email or principal.upn or "").strip().lower()
    if not email:
        email = (
            f"{principal.username}@{settings.ldap_domain.lower()}"
            if settings.ldap_domain
            else f"{principal.username}@ad.invalid"
        )
    return email[:320]


def _role_for(principal: ADPrincipal) -> Role:
    """Роль нового пользователя: по группам AD, иначе — значение по умолчанию."""
    groups = list(principal.groups or [])
    if not groups:
        groups = [ad._cn_from_dn(dn) for dn in principal.group_dns]
    return ad.role_from_groups(groups)


async def find_existing(session: AsyncSession, principal: ADPrincipal) -> User | None:
    """Ищем учётку по доменному логину или любому из e-mail'ов (включая заведённую вручную)."""
    from sqlalchemy import or_

    login_id = ad.build_login_id(principal)
    conds = [User.domain_account == login_id]
    emails = candidate_emails(principal)
    if emails:
        conds.append(func.lower(User.email).in_(emails))

    row = await session.execute(select(User).where(or_(*conds)).limit(1))
    return row.scalar_one_or_none()


async def sync_user(session: AsyncSession, user: User, principal: ADPrincipal) -> bool:
    """Обновляет атрибуты и группы из AD. Возвращает True, если роль была повышена."""
    changed = False

    if user.auth_source != AUTH_LDAP:
        user.auth_source = AUTH_LDAP
        changed = True
    if user.password_hash != UNUSABLE_PASSWORD:
        user.password_hash = UNUSABLE_PASSWORD
        changed = True

    login_id = ad.build_login_id(principal)
    if user.domain_account != login_id:
        user.domain_account = login_id
    if principal.dn and user.dn != principal.dn:
        user.dn = principal.dn[:512]

    if settings.ldap_sync_groups_on_login:
        groups = sorted({g for g in (principal.groups or []) if g})
        if (user.ad_groups or []) != groups:
            user.ad_groups = groups

    email = _email(principal)
    dup = await session.execute(
        select(func.count()).select_from(User).where(func.lower(User.email) == email, User.id != user.id)
    )
    if email and user.email.lower() != email and int(dup.scalar_one()) == 0:
        user.email = email
        changed = True

    if settings.ldap_sync_attrs_on_login:
        full_name = _display_name(principal)
        if full_name and user.full_name != full_name:
            user.full_name = full_name
        position = (principal.position or "").strip()[:255]
        if position:
            from app.services.positions import resolve_position_texts

            pos = (await resolve_position_texts(session, [position]))[position]
            if pos not in (user.positions or []):
                user.positions = (user.positions or []) + [pos]
                changed = True
        phone = (principal.phone or "").strip()[:40]
        if phone and user.phone != phone:
            user.phone = phone

    user.last_ad_sync_at = datetime.now(timezone.utc)

    # роль из групп AD: только ПОВЫШЕНИЕ (понижение — вручную, иначе исключение из группы
    # молча лишило бы сотрудника доступа к разделам)
    target = _role_for(principal)
    role_changed = False
    if settings.ldap_role_from_groups == "upgrade" and ROLE_LEVEL[target] > ROLE_LEVEL[user.role]:
        logger.info("AD: повышаем роль %s: %s → %s (по группам)", user.email, user.role.value, target.value)
        user.role = target
        role_changed = True
        changed = True

    if changed or role_changed:
        await session.flush()
    return role_changed


async def authenticate_principal(session: AsyncSession, username: str, password: str) -> ADPrincipal:
    """Единственная точка входа в каталог: bind + проверка белого списка групп.

    Здесь же нормализуется логин и применяется политика доступа, поэтому
    поведение одинаково для любой реализации `ADProvider`.
    """
    provider = ad.get_provider()
    principal = await provider.authenticate(ad.normalize_username(username), password)
    ad.enforce_group_whitelist(principal)
    return principal


async def provision(session: AsyncSession, principal: ADPrincipal) -> ProvisionResult:
    """Найти/создать учётку SchoolHub для доменного пользователя."""
    user = await find_existing(session, principal)

    if user is None:
        if settings.ldap_provisioning != "auto":
            raise forbidden(
                "Учётная запись в SchoolHub ещё не заведена. Попросите администратора импортировать "
                "вас из Active Directory (раздел «Пользователи» → «Импорт из AD»)."
            )
        user = User(
            email=_email(principal),
            password_hash=UNUSABLE_PASSWORD,
            full_name=_display_name(principal),
            phone=(principal.phone or None),
            role=_role_for(principal),
            is_active=True,
            auth_source=AUTH_LDAP,
            domain_account=ad.build_login_id(principal),
            positions=[],
            dn=principal.dn[:512] if principal.dn else None,
            ad_groups=sorted({g for g in (principal.groups or []) if g}),
            last_ad_sync_at=datetime.now(timezone.utc),
        )
        pos_text = (principal.position or "").strip()[:255]
        if pos_text:
            from app.services.positions import resolve_position_texts

            user.positions = [(await resolve_position_texts(session, [pos_text]))[pos_text]]
        session.add(user)
        await session.flush()
        logger.info("AD: создана учётка %s (%s)", user.email, user.role.value)
        return ProvisionResult(user=user, created=True)

    role_changed = await sync_user(session, user, principal)
    return ProvisionResult(user=user, created=False, role_changed=role_changed)


# --------------------------------------------------------------------- импорт
async def search_ad_users(query: str, limit: int = 50) -> list[ADPrincipal]:
    if not ad.ad_enabled():
        raise forbidden("Интеграция с Active Directory не настроена (LDAP_ENABLED=false)")
    return await ad.get_provider().search_users(query, limit=limit)


async def import_ad_users(
    session: AsyncSession, actor: User, usernames: list[str], role: Role | None = None, request: Request | None = None
) -> dict:
    """Заводит учётки SchoolHub для выбранных доменных пользователей (до их первого входа).

    Нужно, чтобы заранее назначить роль (например, сделать завуча Администрацией) —
    иначе при первом входе он получит роль Учителя.
    """
    if not ad.ad_enabled():
        raise forbidden("Интеграция с Active Directory не настроена (LDAP_ENABLED=false)")
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Импорт из AD доступен Владельцу и Администрации")
    from app.security import can_manage_role

    if role is not None and not can_manage_role(actor.role, role):
        raise forbidden("Недостаточно прав для назначения такой роли")

    provider = ad.get_provider()
    created: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    seen: set[str] = set()
    for raw in usernames[:200]:                      # защита от слишком большого запроса
        login_id = normalize_username(raw).lower()
        if not login_id or login_id in seen:
            continue                                 # дубли в списке — игнорируем (импорт идемпотентен)
        seen.add(login_id)

        existing = await session.execute(
            select(User).where(User.domain_account == login_id).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            skipped.append(login_id)
            continue

        try:
            principals = await provider.search_users(login_id, limit=5)
        except Exception as exc:                    # noqa: BLE001 — ошибка каталога не должна ронять импорт
            failed.append({"username": login_id, "error": str(exc)[:200]})
            continue

        principal = next((p for p in principals if ad.build_login_id(p) == login_id), principals[0] if principals else None)
        if principal is None:
            failed.append({"username": login_id, "error": "не найден в каталоге"})
            continue

        user = User(
            email=_email(principal),
            password_hash=UNUSABLE_PASSWORD,
            full_name=_display_name(principal),
            phone=(principal.phone or None),
            role=role or _role_for(principal),
            is_active=True,
            auth_source=AUTH_LDAP,
            domain_account=ad.build_login_id(principal),
            positions=[],
            dn=principal.dn[:512] if principal.dn else None,
            ad_groups=sorted({g for g in (principal.groups or []) if g}),
            last_ad_sync_at=datetime.now(timezone.utc),
        )
        pos_text = (principal.position or "").strip()[:255]
        if pos_text:
            from app.services.positions import resolve_position_texts

            user.positions = [(await resolve_position_texts(session, [pos_text]))[pos_text]]
        session.add(user)
        await session.flush()
        created.append(user.email)

    await audit.write_audit(
        session,
        actor=actor,
        action="AD_IMPORT",
        entity_type="user",
        changes={"created": created, "skipped": skipped, "failed": failed},
        request=request,
    )
    return {"created": created, "skipped": skipped, "failed": failed, "total": len(created)}
