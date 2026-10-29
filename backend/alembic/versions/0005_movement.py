"""Движение обучающихся + audit_log.actor_name + users.movement_access.

Revision ID: 0005_movement
Revises: 0004_incoming_journal
Create Date: 2026-10-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_movement"
down_revision: Union[str, None] = "0004_incoming_journal"
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
    if not _has_column("users", "movement_access"):
        op.add_column("users", sa.Column("movement_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("audit_log", "actor_name"):
        op.add_column("audit_log", sa.Column("actor_name", sa.String(length=255), nullable=True))
    if not _has_table("movement_orders"):
        op.create_table(
            "movement_orders",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("order_type", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="NEW"),
            sa.Column("guardian", sa.String(length=255), nullable=False),
            sa.Column("student", sa.String(length=255), nullable=False),
            sa.Column("birth_date", sa.Date(), nullable=False),
            sa.Column("from_place", sa.String(length=255), nullable=True),
            sa.Column("to_place", sa.String(length=255), nullable=True),
            sa.Column("order_date", sa.Date(), nullable=False),
            sa.Column("order_number", sa.String(length=120), nullable=False),
            sa.Column("signatory", sa.String(length=255), nullable=True),
            sa.Column("note", sa.String(length=1000), nullable=True),
            sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_movement_orders_order_type", "movement_orders", ["order_type"])
        op.create_index("ix_movement_orders_status", "movement_orders", ["status"])


def downgrade() -> None:
    if _has_table("movement_orders"):
        op.drop_index("ix_movement_orders_status", table_name="movement_orders")
        op.drop_index("ix_movement_orders_order_type", table_name="movement_orders")
        op.drop_table("movement_orders")
    if _has_column("audit_log", "actor_name"):
        op.drop_column("audit_log", "actor_name")
    if _has_column("users", "movement_access"):
        op.drop_column("users", "movement_access")
