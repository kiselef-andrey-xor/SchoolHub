"""Журнал входящих писем: incoming_letters, users.incoming_journal_access, tasks.incoming_number.

Revision ID: 0004_incoming_journal
Revises: 0003_user_deleted_must_change
Create Date: 2026-10-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_incoming_journal"
down_revision: Union[str, None] = "0003_user_deleted_must_change"
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


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_column("users", "incoming_journal_access"):
        op.add_column(
            "users", sa.Column("incoming_journal_access", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    if not _has_column("tasks", "incoming_number"):
        op.add_column("tasks", sa.Column("incoming_number", sa.String(length=120), nullable=True))
    if not _has_table("incoming_letters"):
        op.create_table(
            "incoming_letters",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("reg_year", sa.Integer(), nullable=False),
            sa.Column("reg_no", sa.Integer(), nullable=False),
            sa.Column("incoming_date", sa.Date(), nullable=False),
            sa.Column("incoming_number", sa.String(length=120), nullable=False),
            sa.Column("sent_date", sa.Date(), nullable=True),
            sa.Column("addressee", sa.String(length=255), nullable=True),
            sa.Column("subject", sa.String(length=500), nullable=False),
            sa.Column("note", sa.String(length=1000), nullable=True),
            sa.Column("responsible_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("reg_year", "reg_no", name="uq_incoming_reg"),
        )
        op.create_index("ix_incoming_letters_reg_year", "incoming_letters", ["reg_year"])
        op.create_index("ix_incoming_letters_incoming_date", "incoming_letters", ["incoming_date"])
        op.create_index("ix_incoming_letters_task_id", "incoming_letters", ["task_id"])


def downgrade() -> None:
    if _has_table("incoming_letters"):
        op.drop_index("ix_incoming_letters_task_id", table_name="incoming_letters")
        op.drop_index("ix_incoming_letters_incoming_date", table_name="incoming_letters")
        op.drop_index("ix_incoming_letters_reg_year", table_name="incoming_letters")
        op.drop_table("incoming_letters")
    if _has_column("tasks", "incoming_number"):
        op.drop_column("tasks", "incoming_number")
    if _has_column("users", "incoming_journal_access"):
        op.drop_column("users", "incoming_journal_access")
