"""Роуты аутентификации.

Поддерживаются два источника учётных данных:
  * **local** — пароль в БД (bcrypt). Нужен для первого Владельца и как break-glass
    на случай недоступности контроллера домена;
  * **ldap**  — Active Directory: bind под учёткой пользователя (пароль не хранится),
    автоматический провижининг и синхронизация атрибутов/групп.

Порядок проверки при входе (важно для безопасности):
  1. учётка найдена по e-mail/логину и `auth_source=local` → проверяем локальный пароль,
     в каталог НЕ обращаемся (нельзя подбирать доменный пароль локальной учёткой);
  2. учётка найдена и `auth_source=ldap` → bind в AD;
  3. учётки нет и AD включён → bind в AD и авто-создание записи.

Дополнительно: троттлинг неудачных попыток (защищает доменные учётки от блокировки
политикой lockout) и единая формулировка ошибки входа (защита от перебора e-mail).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, or_, select

from app import capabilities as caps_mod
from app.capabilities import capabilities_for
from app.config import settings
from app.enums import Role
from app.errors import AppError, bad_request, forbidden, unauthorized
from app.models.user import UNUSABLE_PASSWORD, User
from app.schemas.user import LoginIn, MeOut, PasswordChangeIn, TokenPair
from app.security import (
    CurrentUser,
    SessionDep,
    client_ip,
    hash_password_async,
    require_role,
    verify_password_async,
)
from app.services import ad, ad_users, audit, sessions, substitutions
from app.services.audit import write_audit_detached
from app.services.rate_limit import login_limiter

logger = logging.getLogger("schoolhub.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------- login
@router.get("/login-options", summary="Способы входа (публично, для экрана логина)")
async def login_options() -> dict:
    """Не отдаёт никаких настроек каталога — только факты, нужные UI."""
    return {
        "ldap_enabled": ad.ad_enabled(),
        "local_enabled": True,
        "login_hint": (
            "Доменная учётная запись: ivanov или ivanov@school.local"
            if ad.ad_enabled()
            else "Введите e-mail или логин и пароль SchoolHub"
        ),
        "password_managed_by": "Active Directory" if ad.ad_enabled() else "SchoolHub",
    }


@router.post("/login", response_model=TokenPair, summary="Вход (локальный или доменный)")
async def login(payload: LoginIn, session: SessionDep, request: Request, response: Response) -> TokenPair:
    ip = client_ip(request)
    login_hint = (payload.email or "").strip()
    login_limiter.check(ip, login_hint)

    try:
        user, source = await _authenticate(session, payload, request, ip)
    except AppError as exc:
        login_limiter.register_failure(ip, login_hint)
        if exc.status_code in (401, 403, 429):
            # отдельная транзакция: запрос завершится ошибкой и его сессия откатится,
            # а след попытки входа в журнале обязан остаться
            await write_audit_detached(
                actor=None,
                action="LOGIN_FAILED",
                changes={"login": login_hint, "reason": str(exc.detail)[:200], "ip": ip},
                request=request,
            )
        raise

    if not user.is_active:
        login_limiter.register_failure(ip, login_hint)
        raise forbidden("Учётная запись заблокирована. Обратитесь к администратору.")

    user.last_login_at = datetime.now(timezone.utc)
    await session.flush()
    login_limiter.register_success(ip, login_hint)
    await audit.write_audit(
        session,
        actor=user,
        action="LOGIN",
        changes={"source": source, "role": user.role.value},
        request=request,
    )
    # refresh-токен — ТОЛЬКО в httpOnly-cookie (XSS его не прочитает),
    # сессия регистрируется в refresh_sessions и может быть отозвана сервером
    access, refresh = await sessions.issue(session, user, request, remember=payload.remember)
    response.set_cookie(sessions.COOKIE_NAME, refresh, **sessions.cookie_options(payload.remember, request))
    return TokenPair(access_token=access, expires_in=settings.access_token_ttl_min * 60)


async def _authenticate(session, payload: LoginIn, request: Request, ip: str) -> tuple[User, str]:
    """Возвращает (пользователь, источник аутентификации)."""
    raw_login = payload.email.strip()
    normalized = ad.normalize_username(raw_login)

    existing = await _find_local_account(session, raw_login, normalized)

    # 1) локальная учётка — проверяем пароль в БД, в каталог не ходим
    if existing is not None and existing.auth_source == ad_users.AUTH_LOCAL:
        if not await verify_password_async(payload.password, existing.password_hash):
            raise unauthorized("Неверное имя пользователя или пароль")
        return existing, "local"

    # 2/3) доменная аутентификация
    if ad.ad_enabled():
        return await _authenticate_ad(session, payload.password, existing, normalized, request)

    # AD выключен — одинаковая формулировка (не раскрываем существование учётки)
    if existing is None:
        raise unauthorized("Неверное имя пользователя или пароль")
    if not await verify_password_async(payload.password, existing.password_hash):
        raise unauthorized("Неверное имя пользователя или пароль")
    return existing, "local"


async def _find_local_account(session, raw_login: str, normalized: str) -> User | None:
    """Ищем учётку по e-mail, логину локальной учётки, доменному логину или UPN."""
    conds = [func.lower(User.email) == raw_login.lower(), func.lower(User.login) == raw_login.lower()]
    if normalized and normalized != raw_login.lower():
        conds.append(func.lower(User.email) == normalized.lower())
        conds.append(func.lower(User.domain_account) == normalized.lower())
    if settings.ldap_domain:
        conds.append(func.lower(User.domain_account) == raw_login.lower())
    row = await session.execute(select(User).where(or_(*conds)).limit(1))
    return row.scalar_one_or_none()


async def _authenticate_ad(
    session, password: str, existing: User | None, normalized: str, request: Request
) -> tuple[User, str]:
    """Bind в AD + провижининг/синхронизация учётки SchoolHub."""
    try:
        principal = await ad_users.authenticate_principal(session, normalized, password)
    except ad.ADError as exc:
        # каталог недоступен — это 503, а не «неверный пароль»: так админ сразу поймёт причину
        logger.error("AD unavailable: %s", exc.detail)
        raise
    except AppError:
        raise                                   # 401/403 из провайдера пробрасываем как есть
    except Exception as exc:
        logger.exception("AD authentication error")
        raise bad_request("Не удалось проверить учётные данные в домене") from exc

    if settings.ldap_provisioning == "manual" and existing is None:
        raise forbidden(
            "Вход разрешён только заведённым в SchoolHub учётным записям (LDAP_PROVISIONING=manual)"
        )

    result = await ad_users.provision(session, principal)
    user = result.user

    if result.created:
        await audit.write_audit(
            session,
            actor=user,
            action="AD_PROVISION",
            entity_type="user",
            entity_id=user.id,
            changes={
                "email": user.email,
                "domain_account": user.domain_account,
                "role": user.role.value,
                "groups": (user.ad_groups or [])[:30],
            },
            request=request,
        )
    elif result.role_changed:
        await audit.write_audit(
            session,
            actor=user,
            action="AD_ROLE_UPGRADE",
            entity_type="user",
            entity_id=user.id,
            changes={"role": user.role.value, "groups": (user.ad_groups or [])[:30]},
            request=request,
        )

    return user, "ldap"


# ------------------------------------------------------------------- refresh
@router.post("/refresh", response_model=TokenPair, summary="Обновление access-токена (ротация refresh в cookie)")
async def refresh(request: Request, response: Response, session: SessionDep) -> TokenPair:
    """Refresh-токен берётся из httpOnly-cookie, РОТИРУЕТСЯ при каждом обмене.

    Повторное предъявление уже заменённого токена (вне льготного окна параллельных
    вкладок) трактуется как кража — семейство сессий отзывается целиком.
    """
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        raise unauthorized("Нет refresh-токена (cookie не найдена)")
    user, access, new_refresh, persistent = await sessions.rotate(session, token, request)
    response.set_cookie(sessions.COOKIE_NAME, new_refresh, **sessions.cookie_options(persistent, request))
    return TokenPair(access_token=access, expires_in=settings.access_token_ttl_min * 60)


@router.get("/me", response_model=MeOut, summary="Текущий пользователь и его возможности")
async def me(user: CurrentUser, session: SessionDep) -> MeOut:
    out = MeOut.model_validate(user)
    caps = capabilities_for(user.role)
    # грант журнала входящих выдаёт Владелец персонально (по умолчанию — только Владелец)
    if user.incoming_journal_access and caps_mod.INCOMING_READ not in caps:
        caps = [*caps, caps_mod.INCOMING_READ]
    if user.movement_access and caps_mod.MOVEMENT_READ not in caps:
        caps = [*caps, caps_mod.MOVEMENT_READ]
    if user.equipment_access and caps_mod.EQUIPMENT_READ not in caps:
        caps = [*caps, caps_mod.EQUIPMENT_READ]
    # кадровик (грант Владельца) видит журнал замещений целиком и удаляет строки
    if user.substitutions_access and caps_mod.SUBSTITUTIONS_READ not in caps:
        caps = [*caps, caps_mod.SUBSTITUTIONS_READ]
    # системный администратор (грант Владельца, v1.12): очередь заявок Helpdesk —
    # видит все заявки, берёт в работу, выполняет и отклоняет. Роль не важна:
    # сисадмином может быть и учитель информатики.
    if user.helpdesk_access and caps_mod.HELPDESK_MANAGE not in caps:
        caps = [*caps, caps_mod.HELPDESK_MANAGE]
    # участник активного замещения: раздел нужен, чтобы подтвердить принятие —
    # передача объектов с v1.11 выполняется только после подтверждения. В списке
    # такой сотрудник видит ТОЛЬКО свои замещения (фильтр в service.list_subs).
    if caps_mod.SUBSTITUTIONS_READ not in caps and             await substitutions.has_live_participation(session, user.id):
        caps = [*caps, caps_mod.SUBSTITUTIONS_READ]
    out.capabilities = caps
    return out


# ------------------------------------------------------------------ password
@router.post("/change-password", status_code=204, summary="Смена собственного пароля (локальные учётки)")
async def change_password(
    payload: PasswordChangeIn, user: CurrentUser, session: SessionDep, request: Request, response: Response
) -> None:
    if user.auth_source == ad_users.AUTH_LDAP or user.password_hash in (UNUSABLE_PASSWORD, "", None):
        raise forbidden(
            "Пароль доменной учётной записи меняется через Active Directory "
            "(Ctrl+Alt+Del → «Сменить пароль» или у администратора домена)"
        )
    if not await verify_password_async(payload.current_password, user.password_hash):
        raise unauthorized("Текущий пароль указан неверно")
    try:
        user.password_hash = await hash_password_async(payload.new_password)
    except ValueError as exc:
        raise bad_request(str(exc))
    user.password_must_change = False
    await session.flush()
    # все refresh-сессии (включая текущую) отзываются — после смены пароля нужен новый вход
    await sessions.revoke_all_for_user(session, user.id, "password_change")
    response.delete_cookie(sessions.COOKIE_NAME, path=sessions.COOKIE_PATH)
    await audit.write_audit(session, actor=user, action="PASSWORD_CHANGE", request=request)


@router.post("/logout", status_code=204, summary="Выход: отзыв refresh-сессии + очистка cookie")
async def logout(user: CurrentUser, session: SessionDep, request: Request, response: Response) -> None:
    """Refresh-сессия отзывается на сервере (refresh_sessions.revoked_at),
    cookie удаляется. Access-токен доживает свои минуты, но без refresh сессия
    на устройстве больше не восстанавливается."""
    await sessions.revoke_family_by_token(session, request.cookies.get(sessions.COOKIE_NAME), "logout")
    response.delete_cookie(sessions.COOKIE_NAME, path=sessions.COOKIE_PATH)
    await audit.write_audit(session, actor=user, action="LOGOUT", request=request)


# ------------------------------------------------------------------ диагностика
@router.get(
    "/ad/diagnostics",
    summary="Диагностика подключения к Active Directory (OWNER/ADMIN)",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def ad_diagnostics(user: CurrentUser) -> dict:
    """Показывает, что настроено и отвечает ли контроллер домена.

    Секреты (пароль сервисной учётки) НЕ возвращаются — только маска.
    """
    if not settings.ldap_enabled:
        return {"enabled": False, "hint": "Установите LDAP_ENABLED=true и параметры каталога в .env"}

    config = {
        "enabled": True,
        "server": settings.ldap_server or None,
        "host": settings.ldap_host or None,
        "port": settings.ldap_port,
        "ssl": settings.ldap_use_ssl,
        "starttls": (not settings.ldap_use_ssl) and settings.ldap_use_starttls,
        "tls_verify": settings.ldap_tls_verify,
        "ca_cert": settings.ldap_ca_cert or None,
        "base_dn": settings.ldap_base_dn or None,
        "bind_dn": settings.ldap_bind_dn or None,
        "bind_password_set": bool(settings.ldap_bind_password),
        "login_format": settings.ldap_login_format,
        "provisioning": settings.ldap_provisioning,
        "default_role": settings.ldap_default_role.value,
        "allowed_groups": settings.ldap_allowed_groups,
        "admin_groups": settings.ldap_admin_groups,
        "owner_groups": settings.ldap_owner_groups,
        "role_from_groups": settings.ldap_role_from_groups,
        "resolve_group_sids": settings.ldap_resolve_group_sids,
        "attr_map": settings.ldap_attr_map,
    }

    problems: list[str] = []
    if not settings.ldap_server:
        problems.append("Не задан LDAP_SERVER (например dc01.school.local:636)")
    if not settings.ldap_base_dn:
        problems.append("Не задан LDAP_BASE_DN (например DC=school,DC=local)")
    if not settings.ldap_bind_dn:
        problems.append("Не задана сервисная учётка LDAP_BIND_DN — поиск пользователей невозможен")
    if not settings.ldap_bind_password:
        problems.append("Не задан LDAP_BIND_PASSWORD")
    if not settings.ldap_use_ssl and not settings.ldap_use_starttls:
        problems.append("Соединение без шифрования: включите LDAP_USE_SSL=true или LDAP_USE_STARTTLS=true")
    if settings.ldap_tls_verify == "none":
        problems.append("LDAP_TLS_VERIFY=none — сертификат DC не проверяется (только для отладки!)")

    ping: dict = {"skipped": True}
    if not problems:
        try:
            ping = await ad.get_provider().ping()
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
            ping = {"ok": False, "error": str(exc)[:300]}

    return {"config": config, "problems": problems, "ping": ping, "requested_by": user.email}
