"""Alembic env: работает и из CLI (sync-движок), и программно из приложения
(передаёт уже открытое async-соединение через config.attributes["connection"]).
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401,E402
from alembic import context

# модели должны быть импортированы, чтобы metadata знала обо всех таблицах
from app.database import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """DATABASE_URL → sync-диалект для CLI-запусков (alembic upgrade/revision)."""
    url = os.getenv("DATABASE_URL") or "sqlite:///./dev.db"
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")


def _opts(connection) -> dict:
    return {
        "compare_type": True,
        # batch-режим обязателен для SQLite (ALTER TABLE там ограничен),
        # на PostgreSQL alembic использует обычные ALTER
        "render_as_batch": connection.dialect.name == "sqlite",
    }


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, **_opts(connection))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(), target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        # программный запуск из app.database.init_db — соединение уже открыто
        _do_run_migrations(connectable)
        return

    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
