"""Локальные учётки без e-mail: users.login, users.avatar, email допускает NULL.

Добавляет то же, что делает идемпотентный шаг m004 из app/migrations.py, но в виде
ревизии Alembic — для баз, которые уже находятся под управлением Alembic
(именно этот путь использует init_db() для боевых БД с таблицей alembic_version).

Все операции защищены проверками «уже есть?» — ревизию безопасно прогонять повторно
и по базе, которую уже успел дополнить шаг m004 (legacy-ветка init_db).

Revision ID: 0002_local_accounts
Revises: 0001_baseline
Create Date: 2026-10-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0002_local_accounts"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c LIMIT 1"
            ),
            {"t": table, "c": column},
        ).first()
        return row is not None
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    return any(i["name"] == index for i in sa.inspect(bind).get_indexes(table))


def _email_is_nullable() -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'email'"
            )
        ).first()
        return bool(row) and row[0] == "YES"
    col = next(c for c in sa.inspect(bind).get_columns("users") if c["name"] == "email")
    return bool(col.get("nullable", True))


def upgrade() -> None:
    if not _has_column("users", "login"):
        op.add_column("users", sa.Column("login", sa.String(length=120), nullable=True))
    if not _has_index("users", "ix_users_login"):
        op.create_index("ix_users_login", "users", ["login"], unique=True)

    if not _has_column("users", "avatar"):
        op.add_column("users", sa.Column("avatar", sa.String(length=512), nullable=True))

    if not _email_is_nullable():
        if op.get_bind().dialect.name == "postgresql":
            op.alter_column(
                "users",
                "email",
                existing_type=sa.String(length=320),
                nullable=True,
            )
        else:
            # SQLite не умеет DROP NOT NULL in-place; legacy-базы на SQLite — только dev,
            # там схема пересоздаётся из metadata (app/migrations.py::m004).
            pass


def downgrade() -> None:
    if _has_index("users", "ix_users_login"):
        op.drop_index("ix_users_login", table_name="users")
    if _has_column("users", "login"):
        op.drop_column("users", "login")
    if _has_column("users", "avatar"):
        op.drop_column("users", "avatar")
    if _email_is_nullable() and op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "users",
            "email",
            existing_type=sa.String(length=320),
            nullable=False,
        )
