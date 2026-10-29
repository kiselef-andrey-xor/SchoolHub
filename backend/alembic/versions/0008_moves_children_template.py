"""Перемещения инвентаря, дети в приказах движения, глобальный шаблон печати.

Revision ID: 0008_moves_children_template
Revises: 0007_equipment
Create Date: 2026-10-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_moves_children_template"
down_revision: Union[str, None] = "0007_equipment"
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
    if not _has_column("setup_state", "movement_template"):
        op.add_column("setup_state", sa.Column("movement_template", sa.Text(), nullable=True))
    if not _has_table("movement_children"):
        op.create_table(
            "movement_children",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("movement_id", sa.Uuid(), sa.ForeignKey("movement_orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("student", sa.String(length=255), nullable=False),
            sa.Column("birth_date", sa.Date(), nullable=True),
            sa.Column("from_place", sa.String(length=255), nullable=True),
            sa.Column("to_place", sa.String(length=255), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_movement_children_movement_id", "movement_children", ["movement_id"])
    if not _has_table("equipment_moves"):
        op.create_table(
            "equipment_moves",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("item_id", sa.Uuid(), sa.ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("from_location", sa.String(length=255), nullable=True),
            sa.Column("to_location", sa.String(length=255), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("move_date", sa.Date(), nullable=False),
            sa.Column("note", sa.String(length=1000), nullable=True),
            sa.Column("actor_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_equipment_moves_item_id", "equipment_moves", ["item_id"])
        op.create_index("ix_equipment_moves_move_date", "equipment_moves", ["move_date"])
    if op.get_bind().dialect.name == "postgresql":
        if not _guardian_nullable():
            op.alter_column("movement_orders", "guardian", existing_type=sa.String(length=2000), nullable=True)
        if not _column_nullable("movement_orders", "birth_date"):
            op.alter_column("movement_orders", "birth_date", existing_type=sa.Date(), nullable=True)
    # backfill children из старых колонок приказа
    import uuid as _uuid

    bind = op.get_bind()
    legacy = bind.execute(sa.text("SELECT id, student, birth_date, from_place, to_place FROM movement_orders")).all()
    for row in legacy:
        cnt = bind.execute(
            sa.text("SELECT COUNT(*) FROM movement_children WHERE movement_id = :mid"), {"mid": str(row[0])}
        ).scalar_one()
        if not cnt and row[1]:
            bind.execute(
                sa.text(
                    "INSERT INTO movement_children (id, movement_id, student, birth_date, from_place, to_place, position) "
                    "VALUES (:id, :mid, :student, :birth, :frm, :to, 0)"
                ),
                {"id": str(_uuid.uuid4()), "mid": str(row[0]), "student": row[1],
                 "birth": row[2], "frm": row[3], "to": row[4]},
            )


def _column_nullable(table: str, column: str) -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return bool(row) and row[0] == "YES"


def _guardian_nullable() -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'movement_orders' AND column_name = 'guardian'"
        )
    ).first()
    return bool(row) and row[0] == "YES"


def downgrade() -> None:
    if _has_table("equipment_moves"):
        op.drop_index("ix_equipment_moves_move_date", table_name="equipment_moves")
        op.drop_index("ix_equipment_moves_item_id", table_name="equipment_moves")
        op.drop_table("equipment_moves")
    if _has_table("movement_children"):
        op.drop_index("ix_movement_children_movement_id", table_name="movement_children")
        op.drop_table("movement_children")
    if _has_column("setup_state", "movement_template"):
        op.drop_column("setup_state", "movement_template")
