"""users.is_deleted + users.password_must_change.

Revision ID: 0003_user_deleted_must_change
Revises: 0002_local_accounts
Create Date: 2026-10-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_user_deleted_must_change"
down_revision: Union[str, None] = "0002_local_accounts"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c LIMIT 1"
            ),
            {"t": table, "c": column},
        ).first()
        return row is not None
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "is_deleted"):
        op.add_column("users", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("users", "password_must_change"):
        op.add_column(
            "users", sa.Column("password_must_change", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    conn = op.get_bind()
    indexes = [i["name"] for i in sa.inspect(conn).get_indexes("users")]
    if "ix_users_is_deleted" not in indexes:
        op.create_index("ix_users_is_deleted", "users", ["is_deleted"], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    indexes = [i["name"] for i in sa.inspect(conn).get_indexes("users")]
    if "ix_users_is_deleted" in indexes:
        op.drop_index("ix_users_is_deleted", table_name="users")
    if _has_column("users", "password_must_change"):
        op.drop_column("users", "password_must_change")
    if _has_column("users", "is_deleted"):
        op.drop_column("users", "is_deleted")
