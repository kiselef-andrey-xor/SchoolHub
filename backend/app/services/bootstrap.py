"""Первоначальная настройка (first-run bootstrap).

Задача — сделать установку максимально простой:
  * `SECRET_KEY` генерируется автоматически и сохраняется в файле рядом с данными,
    поэтому администратору НЕ нужно ничего придумывать и прописывать в .env;
  * первая учётная запись Владельца создаётся либо через веб-страницу «Настройка»
    (одноразовый код из журнала контейнера), либо одной командой в терминале;
  * пока Владелец не создан, приложение работает в режиме настройки: публично
    доступны только `GET /health` и `/setup/*`.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.errors import conflict, forbidden
from app.models.setup import SetupState
from app.models.user import User
from app.security import hash_password_async

logger = logging.getLogger("schoolhub.bootstrap")

MIN_PASSWORD = 8


def _generate_setup_token() -> str:
    """Короткий код (8 символов, без неоднозначных символов) — его легко продиктовать/скопировать."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ------------------------------------------------------------------ SECRET_KEY
# Генерация/чтение ключа подписи JWT выполняется в app/config.py::_resolve_secret_key
# (там же — порядок ENV → файл → автогенерация). Здесь остаётся только логика настройки.


# ------------------------------------------------------------------ setup state
async def get_setup_state(session: AsyncSession) -> SetupState:
    """Единственная строка состояния установки (создаётся лениво)."""
    from app.config import settings

    state = await session.get(SetupState, SetupState.SINGLETON_ID)
    if state is None:
        state = SetupState(
            id=SetupState.SINGLETON_ID,
            setup_token=(settings.setup_token or "").strip()
            or _generate_setup_token(),
        )
        session.add(state)
        await session.flush()
        logger.info("Код первоначальной настройки: %s", state.setup_token)
    return state


async def _setup_table_exists(session: AsyncSession) -> bool:
    """На первом старте (до init_db/миграций) таблицы может не быть — это не ошибка, а «не настроено»."""
    from sqlalchemy import inspect

    def _check(sync_conn) -> bool:
        return bool(inspect(sync_conn).has_table("setup_state"))

    try:
        conn = await session.connection()
        return bool(await conn.run_sync(_check))
    except Exception:  # pragma: no cover
        return False


async def is_configured(session: AsyncSession) -> bool:
    if not await _setup_table_exists(session):
        return False
    state = await get_setup_state(session)
    if state.completed_at is not None:
        return True
    # считаем настроенным, если активный Владелец уже есть (например, создан через seed/CLI)
    owners = int(
        (
            await session.execute(
                select(func.count()).select_from(User).where(User.role == Role.OWNER, User.is_active.is_(True))
            )
        ).scalar_one()
    )
    return owners > 0


def print_setup_banner(token: str) -> None:
    """Баннер в журнал контейнера — его копирует администратор на странице настройки."""
    line = "=" * 66
    logger.info(line)
    logger.info(" SchoolHub: требуется первоначальная настройка")
    logger.info(" 1) Откройте веб-интерфейс в браузере")
    logger.info(" 2) Введите код настройки: %s", token)
    logger.info(" 3) Создайте учётную запись Владельца")
    logger.info("")
    logger.info(" Или создайте Владельца одной командой в терминале NAS:")
    logger.info("   docker exec -it schoolhub-backend python -m scripts.first_owner \\")
    logger.info("       --email admin@school.local --password '<пароль>' --name 'Администратор'")
    logger.info(line)


# ------------------------------------------------------------------- first owner
@dataclass(slots=True)
class OwnerCredentials:
    email: str
    full_name: str
    password: str
    school_name: str | None = None


async def create_first_owner(session: AsyncSession, data: OwnerCredentials, *, token: str | None = None) -> User:
    """Создаёт Владельца. Вызывается из веб-формы (с кодом) или из CLI (без кода)."""
    state = await get_setup_state(session)

    if state.completed_at is not None:
        raise conflict("Система уже настроена: войдите под своей учётной записью")

    if token is not None and token.strip().upper() != (state.setup_token or "").upper():
        raise forbidden("Неверный код настройки. Скопируйте его из журнала контейнера (Container Manager → backend → Журнал)")

    email = (data.email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise forbidden("Укажите корректный e-mail")
    if len(data.password or "") < MIN_PASSWORD:
        raise forbidden(f"Пароль должен быть не короче {MIN_PASSWORD} символов")

    duplicate = (
        await session.execute(select(func.count()).select_from(User).where(func.lower(User.email) == email))
    ).scalar_one()
    if int(duplicate) > 0:
        raise conflict("Пользователь с таким e-mail уже существует")

    owner = User(
        email=email,
        password_hash=await hash_password_async(data.password),
        full_name=(data.full_name or "Администратор").strip()[:255],
        role=Role.OWNER,
        is_active=True,
        auth_source="local",
    )
    owner.positions = []
    session.add(owner)

    state.completed_at = datetime.now(timezone.utc)
    state.completed_by_email = email
    state.setup_token = None                     # код больше не нужен и не должен остаться в БД
    if data.school_name:
        state.school_name = data.school_name.strip()[:200]

    await session.flush()
    logger.info("Первоначальная настройка завершена, создан Владелец: %s", email)
    return owner


async def ensure_owner_from_env(session: AsyncSession) -> None:
    """Служебный путь: создать Владельца из переменных окружения (для docker-run без веба)."""
    email = (os.getenv("OWNER_EMAIL") or "").strip()
    password = os.getenv("OWNER_PASSWORD") or ""
    if not email or not password:
        return
    if await is_configured(session):
        logger.info("OWNER_EMAIL задан, но система уже настроена — пропускаем")
        return
    await create_first_owner(
        session,
        OwnerCredentials(
            email=email,
            password=password,
            full_name=os.getenv("OWNER_NAME") or "Администратор",
            school_name=os.getenv("SCHOOL_NAME"),
        ),
        token=None,
    )
    await session.commit()
    logger.info("Владелец создан из переменных окружения: %s", email)


def owner_id_from_token(token: str) -> UUID | None:  # pragma: no cover - утилита
    try:
        return UUID(token)
    except ValueError:
        return None
