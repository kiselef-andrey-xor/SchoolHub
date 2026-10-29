"""Управление refresh-сессиями: выпуск, ротация, отзыв.

Refresh-токен больше не живёт «сам по себе» 14 дней:
  * каждый выданный токен — строка `refresh_sessions` (jti уникален);
  * обновление токена РОТИРУЕТ его: старый jti помечается superseded, новый
    выпускается в том же семействе (family_id);
  * предъявление уже заменённого jti вне льготного окна (параллельные вкладки)
    трактуется как кража — семейство отзывается целиком (reuse_detected);
  * выход, смена/сброс пароля и блокировка учётки отзывают сессии немедленно.

Токен передаётся в httpOnly-cookie (см. cookie_options), поэтому XSS не может
его прочитать; SameSite=strict закрывает CSRF на /auth/refresh.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import unauthorized
from app.models.session import RefreshSession
from app.models.user import User
from app.security import REFRESH, create_access_token, create_refresh_token, decode_token
from app.services import audit

COOKIE_NAME = settings.refresh_cookie_name
COOKIE_PATH = f"{settings.api_prefix}/auth"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cookie_secure(request: Request | None) -> bool:
    """Флаг Secure для cookie.

    COOKIE_SECURE=true/false задаётся явно; значение по умолчанию `auto` определяет
    HTTPS по схеме запроса или заголовку от обратного прокси (X-Forwarded-Proto),
    если включён TRUST_PROXY_HEADERS.
    """
    raw = settings.cookie_secure
    if isinstance(raw, bool):
        return raw
    if str(raw).lower() in ("true", "1", "yes"):
        return True
    if str(raw).lower() in ("false", "0", "no"):
        return False
    # auto
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    if settings.trust_proxy_headers:
        return (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"
    return False


def cookie_options(persistent: bool, request: Request | None = None) -> dict[str, Any]:
    """Параметры Set-Cookie для refresh-токена.

    httpOnly  — недоступен документу (XSS не украдёт);
    samesite  — strict: cookie не уходит при межсайтовых запросах (CSRF);
    secure    — автоматически при работе по HTTPS (или явно COOKIE_SECURE=true);
    path      — только /api/v1/auth: прочие эндпоинты cookie не получают;
    max_age   — отсутствует для «сессии браузера» (remember=false).
    """
    opts: dict[str, Any] = {
        "httponly": True,
        "secure": _cookie_secure(request),
        "samesite": settings.cookie_same_site,
        "path": COOKIE_PATH,
    }
    if persistent:
        opts["max_age"] = settings.refresh_token_ttl_days * 86400
    return opts


async def issue(
    db: AsyncSession, user: User, request: Request | None, remember: bool = True
) -> tuple[str, str]:
    """Вход: создаёт семейство сессии и первую пару токенов."""
    jti = uuid.uuid4().hex
    now = utcnow()
    row = RefreshSession(
        user_id=user.id,
        family_id=uuid.uuid4(),
        jti=jti,
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        persistent=remember,
        ip_address=request.client.host if request and request.client else None,
        user_agent=(request.headers.get("user-agent", "")[:512] if request else None),
    )
    db.add(row)
    await db.flush()
    await _prune(db)
    return create_access_token(user), create_refresh_token(user, jti)


async def rotate(
    db: AsyncSession, token: str, request: Request | None = None
) -> tuple[User, str, str, bool]:
    """Обмен refresh-токена на новую пару с РОТАЦИЕЙ.

    Возвращает (user, access, refresh, persistent). Ошибки — 401 (токен
    недействителен) либо отзыв семейства при обнаружении повторного использования.
    """
    payload = decode_token(token, REFRESH)
    jti = str(payload.get("jti") or "")
    row = (
        await db.execute(select(RefreshSession).where(RefreshSession.jti == jti))
    ).scalar_one_or_none()
    if row is None:
        raise unauthorized("Refresh-токен недействителен")

    now = utcnow()
    if row.revoked_at is not None:
        raise unauthorized("Сессия отозвана — войдите заново")

    user = row.user
    if user is None:
        raise unauthorized("Пользователь не найден")
    if not user.is_active:
        raise unauthorized("Учётная запись заблокирована")

    # Срок действия проверяется ОТДЕЛЬНО и ДО ветки про ротацию: раньше это был
    # `elif`, и для токена в льготном окне проверка `expires_at` пропускалась вовсе.
    # Практически не эксплуатируемо (окно секунды, TTL токена — дни), но логически
    # два независимых условия не должны быть связаны в цепочку (review/REVIEW.md §5.9).
    if _aware(row.expires_at) < now:
        raise unauthorized("Срок действия refresh-токена истёк")

    if row.superseded_at is not None:
        age = (now - _aware(row.superseded_at)).total_seconds()
        if age > settings.refresh_reuse_grace_sec:
            # повторное предъявление давно заменённого токена — вероятная кража:
            # отзываем всё семейство. Важно: запрос сейчас завершится ошибкой 401 и
            # его транзакция откатится, поэтому отзыв и аудит выполняем В ОТДЕЛЬНОЙ
            # транзакции (иначе «украденный» токен оставался бы рабочим).
            await _revoke_family_detached(row.family_id, "reuse_detected")
            await audit.write_audit_detached(
                actor=user, action="TOKEN_REUSE_DETECTED", entity_type="user", entity_id=user.id,
                changes={"family": str(row.family_id), "jti": jti}, request=request,
            )
            raise unauthorized("Обнаружено повторное использование refresh-токена — сессия отозвана")
        # льготное окно: параллельная вкладка успела отправить старую cookie —
        # выпускаем ещё одну ветку того же семейства без отзыва

    if row.superseded_at is None:
        row.superseded_at = now

    new_jti = uuid.uuid4().hex
    db.add(
        RefreshSession(
            user_id=row.user_id,
            family_id=row.family_id,
            jti=new_jti,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
            persistent=row.persistent,
            ip_address=request.client.host if request and request.client else row.ip_address,
            user_agent=(request.headers.get("user-agent", "")[:512] if request else row.user_agent),
        )
    )
    await db.flush()
    await _prune(db)
    return user, create_access_token(user), create_refresh_token(user, new_jti), row.persistent


async def revoke_family_by_token(db: AsyncSession, token: str | None, reason: str) -> int:
    """Отозвать семейство по предъявленному токену (выход). Ошибки декодирования игнорируются."""
    if not token:
        return 0
    try:
        payload = decode_token(token, REFRESH)
    except Exception:  # noqa: BLE001 — токен уже мог истечь, выход всё равно фиксируем
        return 0
    jti = str(payload.get("jti") or "")
    row = (
        await db.execute(select(RefreshSession).where(RefreshSession.jti == jti))
    ).scalar_one_or_none()
    if row is None:
        return 0
    return await _revoke_family_rows(db, row.family_id, reason)


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID, reason: str) -> int:
    """Отзыв ВСЕХ refresh-сессий пользователя: смена/сброс пароля, блокировка."""
    result = await db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revocation_reason=reason[:60])
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return int(result.rowcount or 0)


async def _revoke_family_detached(family_id: uuid.UUID, reason: str) -> int:
    """Отзыв семейства в отдельной транзакции (переживает откат запроса при 401)."""
    from app.database import SessionLocal

    factory = audit.session_factory_override or SessionLocal
    async with factory() as own:
        count = await _revoke_family_rows(own, family_id, reason)
        await own.commit()
    return count


async def _revoke_family_rows(db: AsyncSession, family_id: uuid.UUID, reason: str) -> int:
    result = await db.execute(
        update(RefreshSession)
        .where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revocation_reason=reason[:60])
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return int(result.rowcount or 0)


async def _prune(db: AsyncSession) -> None:
    """Уборка мёртвых строк: истекшие/отозванные старше 30 дней."""
    cutoff = utcnow() - timedelta(days=30)
    await db.execute(
        delete(RefreshSession)
        .where((RefreshSession.expires_at < cutoff) | (RefreshSession.revoked_at < cutoff))
        .execution_options(synchronize_session=False)
    )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
