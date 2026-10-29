"""Учёт оборудования: equipment_items + users.equipment_access.

Revision ID: 0007_equipment
Revises: 0006_movement_print_text
Create Date: 2026-10-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_equipment"
down_revision: Union[str, None] = "0006_movement_print_text"
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
    if not _has_column("users", "equipment_access"):
        op.add_column("users", sa.Column("equipment_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_table("equipment_items"):
        op.create_table(
            "equipment_items",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("inventory_number", sa.String(length=120), nullable=True),
            sa.Column("serial_number", sa.String(length=120), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("location", sa.String(length=255), nullable=True),
            sa.Column("note", sa.String(length=1000), nullable=True),
            sa.Column("created_by_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_equipment_items_inventory_number", "equipment_items", ["inventory_number"])


def downgrade() -> None:
    if _has_table("equipment_items"):
        op.drop_index("ix_equipment_items_inventory_number", table_name="equipment_items")
        op.drop_table("equipment_items")
    if _has_column("users", "equipment_access"):
        op.drop_column("users", "equipment_access")
