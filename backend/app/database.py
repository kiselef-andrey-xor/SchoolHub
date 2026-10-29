"""Async SQLAlchemy engine / session / declarative base."""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Unicode-aware поиск на SQLite (в PostgreSQL используется нативный ILIKE)
from app.dbutils import register_sqlite_functions  # noqa: E402

register_sqlite_functions(engine)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI-зависимость: одна сессия на запрос."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Приведение схемы БД к актуальному состоянию (Alembic).

    Три сценария (определяются автоматически, вручную ничего делать не нужно):
      1. ПУСТАЯ база            → `alembic upgrade head` (базовая ревизия создаёт всё);
      2. ЛЕГАСИ-база (создана create_all до Alembic, есть таблицы, нет alembic_version)
                                → добираем отсутствующие таблицы, прогоняем старые
                                  идемпотентные шаги (m001–m003) и штампуем head;
      3. база ПОД Alembic       → `alembic upgrade head` (новые ревизии).

    AUTO_MIGRATE=false отключает автоприменение (для больших инсталляций,
    где миграции накатывает DBA: `cd backend && alembic upgrade head`).
    """
    import logging
    from pathlib import Path

    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    from alembic import command
    from app import models  # noqa: F401  (регистрация mapper'ов)
    from app.config import settings
    from app.migrations import run_migrations

    if not settings.auto_migrate:
        logging.getLogger("schoolhub").info("AUTO_MIGRATE=false — схема не изменяется")
        return

    backend_dir = Path(__file__).resolve().parent.parent

    def _cfg(sync_conn) -> Config:
        cfg = Config(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        cfg.attributes["connection"] = sync_conn
        return cfg

    log = logging.getLogger("schoolhub")
    async with engine.begin() as conn:
        state = await conn.run_sync(
            lambda sc: {
                "tables": set(inspect(sc).get_table_names()),
                "revision": MigrationContext.configure(sc).get_current_revision(),
            }
        )
        legacy = state["revision"] is None and bool(state["tables"])
        if legacy:
            # до Alembic схему вёл create_all + самописные шаги: добираем таблицы,
            # переносим данные старыми миграциями и штампуем текущий head
            await conn.run_sync(lambda sc: Base.metadata.create_all(sc, checkfirst=True))
            applied = await run_migrations(conn)
            if applied:
                log.info("Legacy schema migrations applied: %s", ", ".join(applied))
            await conn.run_sync(lambda sc: command.stamp(_cfg(sc), "head"))
            log.info("Legacy database stamped at alembic head")
        else:
            before = state["revision"]
            await conn.run_sync(lambda sc: command.upgrade(_cfg(sc), "head"))
            # страховка: идемпотентные шаги m001–m004 применяются и в alembic-ветке —
            # если какая-то ревизия не покрыла изменение схемы, шаг добавит его сам
            applied = await run_migrations(conn)
            if applied:
                log.info("Migrations applied after alembic upgrade: %s", ", ".join(applied))
            if before != state["revision"] or before is None:
                log.info("Alembic: schema at head (was: %s)", before or "empty")
