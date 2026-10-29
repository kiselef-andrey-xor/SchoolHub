"""movement_orders.print_text — редактируемая печатная форма.

Revision ID: 0006_movement_print_text
Revises: 0005_movement
Create Date: 2026-10-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_movement_print_text"
down_revision: Union[str, None] = "0005_movement"
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
    if not _has_column("movement_orders", "print_text"):
        op.add_column("movement_orders", sa.Column("print_text", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("movement_orders", "print_text"):
        op.drop_column("movement_orders", "print_text")
