"""Аутентификация и авторизация (RBAC) на уровне API.

Токены: access (короткий) + refresh (длинный, типизированный claim'ом `typ`).
Доступ к эндпоинту описывается декларативно:

    router.get("/x", dependencies=[Depends(require_role(Role.ADMIN))])
    user = Depends(get_current_user)     # любая аутентифицированная роль

Т.к. зависимости FastAPI выполняются до хендлера, неавторизованный запрос
не доходит до бизнес-логики — разграничение на уровне API гарантировано.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.enums import ROLE_LEVEL, Role
from app.errors import forbidden, unauthorized
from app.models.user import User
from app.types import enum_value

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")

ACCESS = "access"
REFRESH = "refresh"


# ---------------------------------------------------------------- passwords
def hash_password(raw: str) -> str:
    if len(raw) < settings.password_min_length:
        raise ValueError(f"Пароль должен быть не короче {settings.password_min_length} символов")
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """Проверка ЛОКАЛЬНОГО пароля. Для доменных учётке хеш = UNUSABLE_PASSWORD → всегда False."""
    from app.models.user import UNUSABLE_PASSWORD

    if not hashed or hashed == UNUSABLE_PASSWORD or hashed.startswith("!"):
        return False
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:  # повреждённый хеш и т.п.
        return False

# ------------------------------------------------------------------ async-обёртки
# bcrypt намеренно медленный: 100–300 мс на вызов при cost=12. Синхронный вызов
# внутри coroutine блокирует event loop на всё это время, а backend работает с
# `--workers 1`: десяток одновременных входов перед уроком выстраивался в очередь
# и вместе с ними «зависали» /health, WebSocket и все остальные запросы.
# В async-хендлерах используйте ЭТИ функции (review/REVIEW.md §5.4).
async def hash_password_async(raw: str) -> str:
    """То же, что hash_password(), но в отдельном потоке — event loop не блокируется."""
    return await asyncio.to_thread(hash_password, raw)


async def verify_password_async(raw: str, hashed: str) -> bool:
    """То же, что verify_password(), но в отдельном потоке — event loop не блокируется."""
    return await asyncio.to_thread(verify_password, raw, hashed)



# -------------------------------------------------------------------- tokens
def _create_token(subject: str | UUID, token_type: str, ttl: timedelta, **extra: Any) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "jti": uuid4().hex,               # уникальный идентификатор токена (deny-list / дедупликация)
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        **extra,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user: User) -> str:
    return _create_token(
        user.id, ACCESS, timedelta(minutes=settings.access_token_ttl_min), role=enum_value(user.role)
    )


def create_refresh_token(user: User, jti: str | None = None) -> str:
    """Refresh-токен с ФИКСИРОВАННЫМ jti: по нему строка в refresh_sessions
    (ротация/отзыв). Без jti — свободный токен (устаревший режим, не используется)."""
    token = _create_token(user.id, REFRESH, timedelta(days=settings.refresh_token_ttl_days))
    if jti is None:
        return token
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "typ": REFRESH,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.refresh_token_ttl_days)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise unauthorized("Срок действия токена истёк")
    except jwt.InvalidTokenError:
        raise unauthorized("Некорректный токен")

    if payload.get("typ") != expected_type:
        raise unauthorized(f"Ожидался токен типа '{expected_type}'")
    if not payload.get("sub"):
        raise unauthorized("Некорректный токен")
    return payload


# --------------------------------------------------------------- dependencies
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDep,
) -> User:
    """Достаёт пользователя из access-токена. 401, если токен отсутствует/невалиден/юзер отключён."""
    if creds is None or not creds.credentials:
        raise unauthorized("Требуется авторизация")

    payload = decode_token(creds.credentials, ACCESS)
    user = await session.get(User, UUID(payload["sub"]))

    if user is None:
        raise unauthorized("Пользователь не найден")
    if not user.is_active:
        raise forbidden("Учётная запись заблокирована")

    # «Последний вход» = последняя АКТИВНОСТЬ: живая сессия тоже обновляет метку,
    # но не чаще раза в 5 минут (одна условная UPDATE-операция по PK).
    now = datetime.now(timezone.utc)
    last = user.last_login_at
    if last is not None and last.tzinfo is None:      # SQLite отдаёт naive-даты
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last) > timedelta(minutes=5):
        user.last_login_at = now

    # «Локдаун»: для всех, кроме Владельца, помечаем запрос как обезличенный —
    # сериализаторы схем подменят персональные данные на метки «Сотрудник N».
    from app.services import lockdown

    masked = await lockdown.is_enabled(session) and user.role != Role.OWNER
    lockdown.set_masking(masked)
    if masked:
        # карта «ФИО -> метка» нужна для обезличивания системных текстов
        # (уведомления, сводки): они собираются строкой и мимо схем не проходят
        await lockdown.prime_names(session)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(min_role: Role, *extra: Role):
    """Фабрика зависимостей: RBAC-гейт «уровень роли НЕ НИЖЕ min_role» (+ явно перечисленные роли).

    Пример: require_role(Role.ADMIN) пропускает ADMIN(2) и OWNER(3), но не TEACHER(1).

    Иерархия: TEACHER(1) < ADMIN(2) < OWNER(3) — см. app/enums.py::ROLE_LEVEL.
    """
    min_level = ROLE_LEVEL[min_role]
    allowed_roles = {min_role, *extra}

    async def dependency(user: CurrentUser) -> User:
        if ROLE_LEVEL[user.role] < min_level and user.role not in allowed_roles:
            raise forbidden(f"Требуется роль {min_role.value} или выше")
        return user

    return dependency


def can_manage_role(actor: Role, target_role: Role) -> bool:
    """Анти-эскалация: нельзя выдать роль ВЫШЕ собственной.

    Роль своего уровня выдавать разрешено только Владельцу (передача владения),
    Администрация может создавать лишь Учителей.
    """
    if actor == Role.OWNER:
        return True
    return ROLE_LEVEL[target_role] < ROLE_LEVEL[actor]


# ------------------------------------------------------------------- helpers
def is_staff(user: User) -> bool:
    """OWNER/ADMIN видят все сущности, TEACHER — только свои."""
    return user.role in (Role.OWNER, Role.ADMIN)


def client_ip(request) -> str:
    """IP клиента. За reverse-proxy (nginx / DSM) берём X-Forwarded-For, но только если
    включён TRUST_PROXY_HEADERS — иначе заголовок можно подделать."""
    if settings.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def client_meta(request) -> dict[str, str]:
    """Метаданные подтверждения ознакомления: IP + User-Agent (юридически значимый след)."""
    return {"ip": client_ip(request), "user_agent": request.headers.get("user-agent", "")[:512]}
