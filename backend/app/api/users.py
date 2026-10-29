"""Пользователи: справочник для назначения задач и управление учётными записями.

Доступ:
  GET  /users          — OWNER, ADMIN (ADMIN не видит владельца)
  POST /users          — OWNER (создание админов), ADMIN (только учителей)
  PATCH /users/{id}    — OWNER; ADMIN — только учителей и без повышения роли
  POST /users/bulk     — массовое создание локальных учётки (таблица «ФИО ПАРОЛЬ»)

Отдельный self_router (без требования ADMIN): аватар и вход по логину — для всех ролей.
"""
from __future__ import annotations

import io
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.config import settings
from app.dbutils import ilike
from app.enums import ROLE_LEVEL, Role
from app.errors import AppError, bad_request, conflict, forbidden, not_found
from app.models.position import Position
from app.models.user import UNUSABLE_PASSWORD, User
from app.schemas.common import Page
from app.schemas.user import (
    LOGIN_RE,
    ADImportIn,
    ADImportOut,
    ADUserOut,
    TotpBreakGlassIn,
    TotpConfirmIn,
    TotpDeleteIn,
    TotpRemoveIn,
    TotpResetIn,
    TotpWordIn,
    UserBulkCreatedOut,
    UserBulkFailedOut,
    UserBulkIn,
    UserBulkOut,
    UserCreateIn,
    UserOut,
    UserUpdateIn,
)
from app.security import (
    CurrentUser,
    SessionDep,
    can_manage_role,
    client_ip,
    hash_password_async,
    require_role,
)
from app.services import ad_users, audit, logins, notifications, totp
from app.services import sessions as session_service
from app.services import storage as _storage_mod
from app.types import enum_value

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_role(Role.ADMIN))])


async def _get_user_or_404(session, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("Пользователь")
    return user


def _visible_scope(actor: User):
    """Админ не управляет и не видит владельца; владелец видит всех."""
    if actor.role == Role.OWNER:
        return None
    return User.role != Role.OWNER


def _assert_can_touch(actor: User, target: User, *, changing_role: Role | None = None) -> None:
    if actor.id == target.id and changing_role and changing_role != actor.role:
        raise conflict("Нельзя изменить собственную роль")

    if actor.role == Role.OWNER:
        return  # владелец может всё

    # ADMIN
    if target.role == Role.OWNER:
        raise forbidden("Управление учётной записью Владельца доступно только Владельцу")
    if changing_role is not None and ROLE_LEVEL[changing_role] >= ROLE_LEVEL[actor.role]:
        raise forbidden("Администрация не может назначать роль своего уровня или выше")


@router.get("", response_model=Page[UserOut], summary="Список пользователей")
async def list_users(
    session: SessionDep,
    actor: CurrentUser,
    search: str | None = Query(default=None, max_length=120),
    role: Role | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> Page[UserOut]:
    stmt = select(User)
    conds = []
    if not (include_deleted and actor.role == Role.OWNER):
        conds.append(User.is_deleted.is_(False))
    scope = _visible_scope(actor)
    if scope is not None:
        conds.append(scope)
    if role:
        conds.append(User.role == role.value)
    if is_active is not None:
        conds.append(User.is_active.is_(is_active))
    if search:
        conds.append(
            or_(
                ilike(User.full_name, search),
                ilike(User.email, search),
                User.positions.any(ilike(Position.title, search)),
                ilike(User.login, search),
            )
        )
    if conds:
        stmt = stmt.where(*conds)

    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await session.execute(stmt.order_by(User.role.desc(), User.full_name).limit(per_page).offset((page - 1) * per_page))
    ).scalars().all()

    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in rows],
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@router.get("/options", response_model=list[UserOut], summary="Компактный список для селектов (исполнители)")
async def user_options(session: SessionDep, actor: CurrentUser, role: Role | None = None) -> list[UserOut]:
    """Справочник для назначения (исполнители, ответственные, круг ознакомления).

    Владельца сюда включаем: администрация должна иметь возможность ставить
    задачи Владельцу (в управленческом списке /users он по-прежнему скрыт от ADMIN).
    """
    stmt = select(User).where(User.is_active.is_(True), User.is_deleted.is_(False))
    if role:
        stmt = stmt.where(User.role == role)
    rows = (await session.execute(stmt.order_by(User.full_name))).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@router.get("/{user_id}", response_model=UserOut, summary="Карточка пользователя")
async def get_user(user_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> UserOut:
    target = await _get_user_or_404(session, user_id)
    if target.is_deleted and actor.role != Role.OWNER:
        raise not_found("Пользователь")
    if actor.role != Role.OWNER and target.role == Role.OWNER:
        raise forbidden()
    return UserOut.model_validate(target)


@router.post("", response_model=UserOut, status_code=201, summary="Создать пользователя")
async def create_user(payload: UserCreateIn, session: SessionDep, actor: CurrentUser) -> UserOut:
    # Право на выдачу роли: нельзя выдать роль своего уровня или выше (анти-эскалация).
    # OWNER -> может создавать OWNER/ADMIN/TEACHER, ADMIN -> только TEACHER.
    if not can_manage_role(actor.role, payload.role):
        raise forbidden("Недостаточно прав для назначения такой роли")

    if payload.login and actor.role != Role.OWNER:
        raise forbidden("Логин вручную может задавать только Владелец")

    email = str(payload.email).lower().strip() if payload.email else None
    if email:
        exists = (
            await session.execute(select(func.count()).select_from(User).where(User.email == email))
        ).scalar_one()
        if exists:
            raise conflict("Пользователь с таким e-mail уже существует")

    login = await _resolve_login(session, payload.login, payload.full_name, email=email)

    user = User(
        email=email,
        login=login,
        password_hash=await hash_password_async(payload.password),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        birth_date=payload.birth_date,
        role=payload.role,
        is_active=True,
    )
    # назначение должностей ДО add: у нового объекта коллекция должна быть
    # инициализирована, иначе сериализация полезет в ленивую загрузку
    user.positions = list(await _positions_by_ids(session, payload.position_ids))
    session.add(user)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="USER_CREATE", entity_type="user", entity_id=user.id,
        changes={"email": user.email, "login": user.login, "role": enum_value(user.role), "full_name": user.full_name},
    )
    return UserOut.model_validate(user)


def _pos_titles(user: User) -> str:
    return ", ".join(p.title for p in (user.positions or []))


async def _positions_by_ids(session, ids) -> list:
    from app.models.position import Position

    unique = list(dict.fromkeys(str(i) for i in ids or []))
    if not unique:
        return []
    rows = (await session.execute(select(Position).where(Position.id.in_(unique)))).scalars().all()
    return list(rows)


async def _resolve_position_texts(session, texts) -> dict:
    """Тексты должностей (массовый импорт/AD) → записи справочника; неизвестные создаются."""
    from app.services.positions import resolve_position_texts

    return await resolve_position_texts(session, texts)


async def _resolve_login(session, manual: str | None, full_name: str, *, email: str | None) -> str | None:
    """Логин локальной учётки: задан вручную | сгенерирован из ФИО (если нет e-mail) | не нужен."""
    if manual:
        value = manual.strip().lower()
        if not LOGIN_RE.match(value):
            raise bad_request("Логин содержит недопустимые символы (разрешены латиница, цифры, . _ - @)")
        dup = (
            await session.execute(select(func.count()).select_from(User).where(User.login == value))
        ).scalar_one()
        if dup:
            raise conflict("Логин уже занят")
        return value
    if email:
        return None                      # вход по e-mail, логин не нужен
    return await logins.generate_login(session, full_name)


@router.patch("/{user_id}", response_model=UserOut, summary="Обновить пользователя (роль, активность, данные)")
async def update_user(
    user_id: uuid.UUID, payload: UserUpdateIn, session: SessionDep, actor: CurrentUser
) -> UserOut:
    target = await _get_user_or_404(session, user_id)
    _assert_can_touch(actor, target, changing_role=payload.role)

    patch = payload.model_dump(exclude_unset=True)
    # «Локдаун»: интерфейсы отдают метки вместо ФИО — чтобы маска не попала в БД,
    # персональные поля в этот период просто не редактируются.
    from app.services import lockdown as lockdown_service

    if lockdown_service.masking():
        for f in ("full_name", "phone", "birth_date", "email", "login"):
            patch.pop(f, None)
    if not patch:
        raise bad_request("Пустой запрос на обновление (в режиме локдауна персональные данные не редактируются)")

    before = {"email": target.email, "login": target.login, "full_name": target.full_name,
              "role": enum_value(target.role),
              "is_active": target.is_active, "positions": _pos_titles(target), "phone": target.phone}

    if "position_ids" in patch:
        ids = patch.pop("position_ids")
        if ids is not None:
            rows = await _positions_by_ids(session, ids)
            if len(rows) != len(set(ids)):
                raise bad_request("Одна из должностей не найдена в справочнике")
            target.positions = list(rows)

    if "password" in patch:
        password = patch.pop("password")
        if password:
            if target.auth_source == "ldap":
                raise conflict(
                    "Пароль доменной учётной записи меняется в Active Directory, а не в SchoolHub"
                )
            target.password_hash = await hash_password_async(password)
            if target.id != actor.id:
                # пароль выдан администратором — сотрудник обязан сменить его при первом входе
                target.password_must_change = True
            # сброс пароля администрацией рвёт ВСЕ сессии пользователя
            await session_service.revoke_all_for_user(session, target.id, "password_reset")

    if "incoming_journal_access" in patch and patch["incoming_journal_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Выдавать доступ к журналу входящих может только Владелец")
        target.incoming_journal_access = bool(patch["incoming_journal_access"])

    if "movement_access" in patch and patch["movement_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Выдавать доступ к разделу «Движение» может только Владелец")
        target.movement_access = bool(patch["movement_access"])

    if "equipment_access" in patch and patch["equipment_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Выдавать доступ к учёту оборудования может только Владелец")
        target.equipment_access = bool(patch["equipment_access"])

    if "notes_full_access" in patch and patch["notes_full_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Выдавать доступ ко всем служебным запискам может только Владелец")
        target.notes_full_access = bool(patch["notes_full_access"])

    if "helpdesk_access" in patch and patch["helpdesk_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Назначать системных администраторов Helpdesk может только Владелец")
        target.helpdesk_access = bool(patch["helpdesk_access"])

    if "substitutions_access" in patch and patch["substitutions_access"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Назначать кадровика (доступ к журналу замещений) может только Владелец")
        target.substitutions_access = bool(patch["substitutions_access"])

    if "password_must_change" in patch and patch["password_must_change"] is not None:
        if actor.role != Role.OWNER:
            raise forbidden("Требовать смену пароля может только Владелец")
        if target.auth_source == "ldap":
            raise conflict("Для доменных учётных записей пароль управляется в Active Directory")
        target.password_must_change = bool(patch["password_must_change"])

    if "login" in patch and patch["login"] is not None and str(patch["login"]).strip():
        new_login = str(patch["login"]).strip().lower()
        if actor.role != Role.OWNER:
            raise forbidden("Менять логин может только Владелец")
        if target.auth_source == "ldap":
            raise conflict("Логин есть только у локальных учётных записей")
        if not LOGIN_RE.match(new_login):
            raise bad_request("Логин содержит недопустимые символы (разрешены латиница, цифры, . _ - @)")
        dup = (
            await session.execute(
                select(func.count()).select_from(User).where(User.login == new_login, User.id != target.id)
            )
        ).scalar_one()
        if dup:
            raise conflict("Логин уже занят")
        target.login = new_login

    if "email" in patch and patch["email"]:
        new_email = str(patch["email"]).lower().strip()
        dup = (
            await session.execute(
                select(func.count()).select_from(User).where(User.email == new_email, User.id != target.id)
            )
        ).scalar_one()
        if dup:
            raise conflict("E-mail уже занят")
        target.email = new_email

    for field in ("full_name", "phone"):
        if field in patch and patch[field] is not None:
            setattr(target, field, str(patch[field]).strip())

    if "role" in patch and patch["role"] is not None:
        new_role = Role(patch["role"]) if not isinstance(patch["role"], Role) else patch["role"]
        if target.id == actor.id and new_role != actor.role:
            raise conflict("Нельзя изменить собственную роль")
        if actor.role == Role.ADMIN and new_role != Role.TEACHER:
            raise forbidden("Администрация может назначать только роль Учителя")
        if target.role == Role.OWNER and actor.role != Role.OWNER:
            raise forbidden("Снять роль Владельца может только Владелец")
        owners_left = (
            await session.execute(
                select(func.count()).select_from(User).where(
                    User.role == Role.OWNER, User.is_active.is_(True), User.id != target.id
                )
            )
        ).scalar_one()
        if target.role == Role.OWNER and new_role != Role.OWNER and owners_left == 0:
            raise conflict("В школе должен остаться хотя бы один активный Владелец")
        target.role = new_role

    if "is_active" in patch and patch["is_active"] is not None:
        if target.id == actor.id and patch["is_active"] is False:
            raise conflict("Нельзя заблокировать собственную учётную запись")
        if target.role == Role.OWNER and patch["is_active"] is False:
            owners_left = (
                await session.execute(
                    select(func.count()).select_from(User).where(
                        User.role == Role.OWNER, User.is_active.is_(True), User.id != target.id
                    )
                )
            ).scalar_one()
            if owners_left == 0:
                raise conflict("Нельзя заблокировать единственного Владельца")
        target.is_active = bool(patch["is_active"])
        if not target.is_active:
            await session_service.revoke_all_for_user(session, target.id, "deactivated")

    await session.flush()
    after = {"email": target.email, "login": target.login, "full_name": target.full_name,
             "role": enum_value(target.role),
             "is_active": target.is_active, "positions": _pos_titles(target), "phone": target.phone}
    await audit.write_audit(
        session, actor=actor, action="USER_UPDATE", entity_type="user", entity_id=target.id,
        changes=audit.diff(before, after),
    )
    return UserOut.model_validate(target)


@router.delete("/{user_id}", status_code=204, summary="Удалить пользователя (только Владелец)")
async def delete_user(
    user_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    payload: TotpDeleteIn | None = Body(
        default=None,
        description='ПЭП «код + слово»: {"code": "123456", "word": "…"}. '
                    "Отсутствие подписи блокируется 409 (TOTP_NOT_BOUND / RECOVERY_WORD_MISSING).",
    ),
) -> None:
    """Удаление БЕЗ потери истории: учётка скрывается из всех списков, вход закрывается,
    e-mail и логин освобождаются; приказы/задачи/ознакомления сотрудника сохраняются.
    Владелец может восстановить учётку (POST /users/{id}/restore).

    ПЭП максимального уровня: тело запроса {"code": "...", "word": "..."} —
    одноразовый код аутентификатора + контрольное слово."""
    if actor.role != Role.OWNER:
        raise forbidden("Удалять учётные записи может только Владелец")
    target = await _get_user_or_404(session, user_id)
    if target.id == actor.id:
        raise conflict("Нельзя удалить собственную учётную запись")
    if target.is_deleted:
        raise conflict("Учётная запись уже удалена")
    if target.role == Role.OWNER:
        owners_left = (
            await session.execute(
                select(func.count()).select_from(User).where(
                    User.role == Role.OWNER, User.is_active.is_(True),
                    User.is_deleted.is_(False), User.id != target.id,
                )
            )
        ).scalar_one()
        if owners_left == 0:
            raise conflict("Нельзя удалить единственного активного Владельца")
    # ПЭП максимального уровня: удаление пользователя требует код + контрольное слово
    # (проверяется после бизнес-ограничений — чтобы «последний Владелец» и т.п.
    # возвращали 409 независимо от наличия подписи)
    await totp.require_code_word(actor, payload.code if payload else None, payload.word if payload else None)
    before = {"full_name": target.full_name, "email": target.email, "login": target.login}
    target.is_deleted = True
    target.is_active = False
    target.email = None          # освобождаем уникальные ключи для повторного использования
    target.login = None
    target.password_hash = UNUSABLE_PASSWORD
    await session_service.revoke_all_for_user(session, target.id, "deleted")
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="USER_DELETE", entity_type="user", entity_id=target.id,
        changes=before, request=request,
    )


@router.post("/{user_id}/restore", response_model=UserOut, summary="Восстановить удалённую учётку (Владелец)")
async def restore_user(user_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> UserOut:
    if actor.role != Role.OWNER:
        raise forbidden("Восстанавливать учётные записи может только Владелец")
    target = await _get_user_or_404(session, user_id)
    if not target.is_deleted:
        raise conflict("Учётная запись не удалена")
    target.is_deleted = False
    target.is_active = False     # включение и новый логин/e-mail — осознанно, через редактирование
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="USER_RESTORE", entity_type="user", entity_id=target.id,
        changes={"full_name": target.full_name},
    )
    return UserOut.model_validate(target)


@router.post("/{user_id}/totp-reset", status_code=204,
             summary="Сбросить аутентификатор сотрудника (Владелец, ПЭП «код + слово»)")
async def reset_user_totp(
    user_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    payload: TotpResetIn | None = Body(
        default=None,
        description='ПЭП Владельца «код + слово»: {"code": "123456", "word": "…"}',
    ),
) -> None:
    """Аварийный путь: сотрудник потерял телефон и не может отвязать аутентификатор сам.

    Сбрасывается только привязка (секрет, контрольное слово, флаги) — учётная запись,
    сессии и история подписей не затрагиваются. Сотрудник получает уведомление,
    а действие пишется в журнал: снятие подписи с чужой учётки должно быть видно.
    """
    if actor.role != Role.OWNER:
        raise forbidden("Сбрасывать аутентификатор сотрудника может только Владелец")
    target = await _get_user_or_404(session, user_id)
    if not target.totp_secret_enc:
        raise conflict("Аутентификатор сотрудника не привязан — сбрасывать нечего")
    # ПЭП максимального уровня: операция снимает с сотрудника электронную подпись
    await totp.require_code_word(actor, payload.code if payload else None, payload.word if payload else None)

    await totp.remove_totp(session, target)
    await audit.write_audit(
        session, actor=actor, action="TOTP_RESET", entity_type="user", entity_id=target.id,
        changes={"full_name": target.full_name, "email": target.email, "login": target.login},
        request=request,
    )
    await notifications.push(
        session,
        user_id=target.id,
        type_=notifications.TOTP_RESET,
        title="Аутентификатор сброшен Владельцем",
        body="Привязка приложения-аутентификатора к вашей учётной записи снята. "
             "Электронная подпись кодом недоступна, пока вы не привяжете аутентификатор заново "
             "(Профиль → «Электронная подпись»). Если это сделали не по вашей просьбе — "
             "немедленно сообщите Владельцу системы.",
        entity_type="user",
        entity_id=target.id,
    )


# ------------------------------------------------------------------ массовое создание
@router.post(
    "/bulk",
    response_model=UserBulkOut,
    status_code=201,
    summary="Массовое создание локальных учётных записей (таблица: ФИО, ПАРОЛЬ[, e-mail[, роль]])",
)
async def bulk_create_users(payload: UserBulkIn, session: SessionDep, actor: CurrentUser) -> UserBulkOut:
    """Строки приходят уже распарсенными клиентом (textarea или CSV/TSV-файл).

    Для каждой строки: проверка прав на роль, уникальность e-mail, генерация логина
    (если e-mail не задан). Ошибка в одной строке не прерывает остальные.
    """
    created: list[UserBulkCreatedOut] = []
    failed: list[UserBulkFailedOut] = []
    taken_logins: set[str] = set()
    taken_emails: set[str] = set()

    for idx, row in enumerate(payload.rows, start=1):
        full_name = row.full_name.strip()
        try:
            if not can_manage_role(actor.role, row.role):
                raise forbidden("Недостаточно прав для назначения такой роли")
            if len(row.password) < settings.password_min_length:
                raise bad_request(f"Пароль короче {settings.password_min_length} символов")
            email = str(row.email).lower().strip() if row.email else None
            if email:
                if email in taken_emails:
                    raise conflict("E-mail дублируется в таблице")
                dup = (
                    await session.execute(select(func.count()).select_from(User).where(User.email == email))
                ).scalar_one()
                if dup:
                    raise conflict("Пользователь с таким e-mail уже существует")
                taken_emails.add(email)
                login = None
            else:
                login = await logins.generate_login(session, full_name, taken=taken_logins)
                taken_logins.add(login)

            user = User(
                email=email,
                login=login,
                # bcrypt — CPU-операция: уводим в поток, чтобы не блокировать event loop
                password_hash=await hash_password_async(row.password),
                full_name=full_name,
                role=row.role,
                is_active=True,
            )
            user.positions = []
            if row.position and row.position.strip():
                pos_map = await _resolve_position_texts(session, {row.position.strip()})
                user.positions = [pos_map[row.position.strip()]]
            session.add(user)
            await session.flush()
            created.append(UserBulkCreatedOut(full_name=full_name, login=login, email=email, role=row.role))
        except AppError as exc:
            failed.append(UserBulkFailedOut(row=idx, full_name=full_name, error=str(exc.detail)))

    await audit.write_audit(
        session, actor=actor, action="USER_BULK_CREATE",
        changes={"created": len(created), "failed": len(failed), "total": len(payload.rows)},
    )
    return UserBulkOut(created=created, failed=failed, total=len(payload.rows))


# ------------------------------------------------------------------ Active Directory
@router.get("/ad/search", response_model=list[ADUserOut], summary="Поиск пользователей в домене (для импорта)")
async def ad_search(
    session: SessionDep,
    actor: CurrentUser,
    q: str = Query(min_length=2, max_length=120, description="Часть ФИО, логина или e-mail"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ADUserOut]:
    """Работает только при настроенной сервисной учётке LDAP (чтение каталога)."""
    principals = await ad_users.search_ad_users(q, limit=limit)
    return [
        ADUserOut(
            username=p.username,
            full_name=p.full_name,
            email=p.email,
            position=p.position or None,
            phone=p.phone or None,
            dn=p.dn or None,
            disabled=p.disabled,
            groups=p.groups[:50],
        )
        for p in principals
    ]


@router.post("/ad/import", response_model=ADImportOut, summary="Импортировать учётки из домена")
async def ad_import(
    payload: ADImportIn, session: SessionDep, actor: CurrentUser, request: Request
) -> ADImportOut:
    """Создаёт записи SchoolHub для выбранных доменных пользователей.

    Нужно, чтобы назначить роль ДО первого входа (иначе пользователь получит роль
    по умолчанию — Учитель). Пароль не создаётся: вход идёт через AD.
    """
    result = await ad_users.import_ad_users(
        session, actor, payload.usernames, role=payload.role, request=request
    )
    return ADImportOut(**result)


# ------------------------------------------------------------------ self-service (все роли)
self_router = APIRouter(prefix="/users", tags=["users"])

AVATAR_MIMES = ["image/jpeg", "image/png", "image/webp"]
AVATAR_MAX_BYTES = 5 * 1024 * 1024


@self_router.post("/me/avatar", response_model=UserOut, summary="Загрузить собственное фото (аватар)")
async def upload_my_avatar(
    session: SessionDep,
    actor: CurrentUser,
    file: UploadFile = File(description="JPEG, PNG или WebP до 5 МБ"),
) -> UserOut:
    data = await file.read(AVATAR_MAX_BYTES + 1)
    if len(data) > AVATAR_MAX_BYTES:
        raise bad_request("Файл больше 5 МБ")
    stored = await _storage_mod.storage.save(io.BytesIO(data), file.filename or "avatar.png", file.content_type or "", AVATAR_MIMES)
    old = actor.avatar
    actor.avatar = stored.storage_path
    await session.flush()
    if old:
        _storage_mod.storage.delete(old)
    await audit.write_audit(session, actor=actor, action="AVATAR_UPDATE", entity_type="user", entity_id=actor.id)
    return UserOut.model_validate(actor)


@self_router.delete("/me/avatar", status_code=204, summary="Удалить собственное фото")
async def delete_my_avatar(session: SessionDep, actor: CurrentUser) -> None:
    old = actor.avatar
    actor.avatar = None
    await session.flush()
    if old:
        _storage_mod.storage.delete(old)
    await audit.write_audit(session, actor=actor, action="AVATAR_DELETE", entity_type="user", entity_id=actor.id)


@self_router.get("/{user_id}/avatar", summary="Фото сотрудника (для отображения в интерфейсе)")
async def get_user_avatar(user_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> FileResponse:
    """Отдаётся любому авторизованному пользователю: аватары видны в списках и приказах."""
    target = await _get_user_or_404(session, user_id)
    if not target.avatar:
        raise not_found("Фото не загружено")
    path = _storage_mod.storage.abspath(target.avatar)
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        path.suffix.lower(), "application/octet-stream"
    )
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------- bulk edit (массовый редактор)
class BulkUpdateIn(BaseModel):
    """Массовое изменение отмеченных сотрудников: должности-надстройка и гранты модулей.

    Гранты и активность меняет только Владелец; должности может добавлять и Администрация
    (с соблюдением тех же ограничений уровней, что и одиночное редактирование)."""

    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    position_ids_add: list[uuid.UUID] | None = None
    position_ids_remove: list[uuid.UUID] | None = None
    incoming_journal_access: bool | None = None
    movement_access: bool | None = None
    equipment_access: bool | None = None
    notes_full_access: bool | None = None
    substitutions_access: bool | None = None
    is_active: bool | None = None


@router.post("/bulk-update", summary="Массовый редактор: применить изменения к отмеченным")
async def bulk_update_users(payload: BulkUpdateIn, session: SessionDep, actor: CurrentUser) -> dict:
    owner_only = (
        payload.incoming_journal_access is not None
        or payload.movement_access is not None
        or payload.equipment_access is not None
        or payload.notes_full_access is not None
        or payload.substitutions_access is not None
        or payload.is_active is not None
    )
    if owner_only and actor.role != Role.OWNER:
        raise forbidden("Гранты модулей и активность меняет только Владелец")
    if not (payload.position_ids_add or payload.position_ids_remove) and not owner_only:
        raise bad_request("Не указано, что изменять")

    add_positions = (
        list((await session.execute(select(Position).where(Position.id.in_(payload.position_ids_add)))).scalars().all())
        if payload.position_ids_add
        else []
    )
    if payload.position_ids_add and len(add_positions) != len(set(payload.position_ids_add)):
        raise bad_request("Одна из должностей не найдена в справочнике")
    remove_ids = set(payload.position_ids_remove or [])

    targets = (
        await session.execute(
            select(User).where(User.id.in_(payload.user_ids), User.is_deleted.is_(False))
        )
    ).scalars().all()
    known = {t.id for t in targets}
    skipped = [{"id": str(uid), "reason": "не найден или удалён"} for uid in payload.user_ids if uid not in known]

    updated = 0
    names: list[str] = []
    for target in targets:
        try:
            _assert_can_touch(actor, target)
        except AppError as exc:
            skipped.append({"id": str(target.id), "reason": exc.detail})
            continue
        if payload.position_ids_add:
            have = {p.id for p in target.positions}
            target.positions = list(target.positions) + [p for p in add_positions if p.id not in have]
        if remove_ids:
            target.positions = [p for p in target.positions if p.id not in remove_ids]
        if payload.incoming_journal_access is not None:
            target.incoming_journal_access = payload.incoming_journal_access
        if payload.movement_access is not None:
            target.movement_access = payload.movement_access
        if payload.equipment_access is not None:
            target.equipment_access = payload.equipment_access
        if payload.notes_full_access is not None:
            target.notes_full_access = payload.notes_full_access
        if payload.substitutions_access is not None:
            target.substitutions_access = payload.substitutions_access
        if payload.is_active is not None and target.id != actor.id:
            target.is_active = payload.is_active
            if not payload.is_active:
                await session_service.revoke_all_for_user(session, target.id, "deactivated_bulk")
        updated += 1
        names.append(target.full_name)
    await session.flush()
    await audit.write_audit(
        session,
        actor=actor,
        action="USER_BULK_UPDATE",
        changes={
            "updated": updated,
            "skipped": len(skipped),
            "positions_add": [p.title for p in add_positions],
            "employees": names[:10],
        },
    )
    return {"updated": updated, "skipped": skipped}


# --------------------------------------------- закрепляемые виды списков (все роли)
ALLOWED_LIST_KEYS = {"orders", "tasks", "incoming", "movement", "equipment", "certifications", "users", "audit", "dashboard"}


class ListViewIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict, max_length=30)


# ------------------------------------------------------------ ПЭП: аутентификатор (TOTP)
@self_router.post("/me/totp/setup", summary="Начать привязку аутентификатора: секрет выдаётся ОДНОРАЗОВО")
async def totp_setup(session: SessionDep, actor: CurrentUser, request: Request) -> dict:
    """Генерирует новый секрет и otpauth-URI для QR.

    Секрет возвращается ТОЛЬКО в этом ответе и больше никогда не отдаётся API.
    Привязка считается завершённой после подтверждения первым кодом
    (POST /users/me/totp/confirm); до этого totp_enabled=False."""
    result = await totp.setup_totp(session, actor)
    await audit.write_audit(
        session, actor=actor, action="TOTP_SETUP_START", entity_type="user", entity_id=actor.id,
        request=request,
    )
    return result


@self_router.post("/me/totp/confirm", status_code=204, summary="Подтвердить привязку первым кодом из приложения")
async def totp_confirm(payload: TotpConfirmIn, session: SessionDep, actor: CurrentUser, request: Request) -> None:
    await totp.confirm_totp(session, actor, payload.code)
    await audit.write_audit(
        session, actor=actor, action="TOTP_CONFIRMED", entity_type="user", entity_id=actor.id,
        request=request,
    )


@self_router.delete("/me/totp", status_code=204,
                    summary="Отвязать аутентификатор (действующая привязка — только текущим кодом)")
async def totp_remove(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    payload: TotpRemoveIn | None = Body(
        default=None,
        description='Текущий код из приложения: {"code": "123456"}. '
                    "Незавершённую привязку можно снять без кода.",
    ),
) -> None:
    """Секрет уничтожается, флаги привязки и контрольное слово сбрасываются.

    Действующая привязка снимается ТОЛЬКО текущим кодом. Иначе тот, кто завладел
    access-токеном (XSS), молча отвязал бы аутентификатор сотрудника, привязал свой
    и подписывал бы приказы от его имени — то есть обнулил бы всю модель ПЭП.
    Если телефон утерян, привязку сбрасывает Владелец в карточке сотрудника.
    """
    if not actor.totp_secret_enc:
        raise bad_request("Аутентификатор не привязан")
    if actor.totp_confirmed_at is not None:
        code = (payload.code if payload else None) or ""
        if not code.strip():
            raise AppError(
                409,
                "Отвязка подтверждается текущим кодом из приложения-аутентификатора. "
                "Если телефон утерян или приложение удалено — попросите Владельца сбросить "
                "привязку в вашей карточке сотрудника (кнопка «Сбросить аутентификатор»)",
                code="TOTP_CODE_REQUIRED",
            )
        await totp.check_totp_code(actor, code)
    await totp.remove_totp(session, actor)
    await audit.write_audit(
        session, actor=actor, action="TOTP_REMOVED", entity_type="user", entity_id=actor.id,
        request=request,
    )


@self_router.post("/me/totp/break-glass", status_code=204,
                  summary="Резервный сброс аутентификатора Владельца (потерян телефон): пароль + контрольное слово")
async def totp_break_glass(
    payload: TotpBreakGlassIn,
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    response: Response,
) -> None:
    """Аварийный выход из тупика «Владелец потерял устройство с аутентификатором».

    Штатный сброс привязки сотрудника подписывает Владелец кодом TOTP + словом;
    но если телефон потерял сам Владелец, подписать ему нечего, а восстановление
    из копии и выход из локдауна тоже требуют код. Без этой процедуры доступ
    возвращался только правкой базы на NAS вручную.

    Подпись заменяется парой факторов: локальный пароль (bcrypt) + контрольное
    слово, заданное ЗАРАНЕЕ и хранящееся зашифрованным вместе с секретом.
    Подбор троттлится отдельным лимитером, каждая неудача пишется в журнал
    отдельной транзакцией, после успеха отзываются ВСЕ сессии Владельца
    (включая текущую) — поэтому cookie снимается здесь же, а интерфейс уводит
    пользователя на экран входа.

    Уведомление уходит Владельцу и всей администрации: снятие подписи с самой
    привилегированной учётной записи должно быть видно не только тому, кто его сделал.
    """
    await totp.break_glass_reset(
        session, actor, payload.password, payload.word, ip=client_ip(request),
    )
    response.delete_cookie(session_service.COOKIE_NAME, path=session_service.COOKIE_PATH)
    await audit.write_audit(
        session, actor=actor, action="TOTP_BREAK_GLASS_DONE", entity_type="user", entity_id=actor.id,
        request=request,
    )


@self_router.put("/me/totp/recovery-word", status_code=204, summary="Установить контрольное слово (уровень «код + слово»)")
async def totp_set_word(payload: TotpWordIn, session: SessionDep, actor: CurrentUser, request: Request) -> None:
    await totp.set_recovery_word(session, actor, payload.word, payload.code)
    await audit.write_audit(
        session, actor=actor, action="TOTP_WORD_SET", entity_type="user", entity_id=actor.id,
        request=request,
    )


@self_router.get("/me/list-views", summary="Мои закреплённые виды списков (фильтры по ключам)")
async def get_list_views(session: SessionDep, actor: CurrentUser) -> dict[str, dict[str, Any]]:
    from app.models.list_view import UserListView

    rows = (
        await session.execute(select(UserListView).where(UserListView.user_id == actor.id))
    ).scalars().all()
    return {r.list_key: r.params for r in rows}


@self_router.put("/me/list-views/{list_key}", summary="Запомнить вид списка (закрепить фильтры)")
async def put_list_view(
    list_key: str, payload: ListViewIn, session: SessionDep, actor: CurrentUser
) -> dict[str, Any]:
    from app.models.list_view import UserListView

    if list_key not in ALLOWED_LIST_KEYS:
        raise bad_request(f"Неизвестный список: {list_key}")
    row = (
        await session.execute(
            select(UserListView).where(UserListView.user_id == actor.id, UserListView.list_key == list_key)
        )
    ).scalars().first()
    if row is None:
        row = UserListView(user_id=actor.id, list_key=list_key, params=payload.params)
        session.add(row)
    else:
        row.params = payload.params
    await session.flush()
    return {"list_key": list_key, "params": row.params}


@self_router.delete("/me/list-views/{list_key}", status_code=204, summary="Сбросить закреплённый вид")
async def delete_list_view(list_key: str, session: SessionDep, actor: CurrentUser) -> None:
    from app.models.list_view import UserListView

    row = (
        await session.execute(
            select(UserListView).where(UserListView.user_id == actor.id, UserListView.list_key == list_key)
        )
    ).scalars().first()
    if row is not None:
        await session.delete(row)
        await session.flush()
