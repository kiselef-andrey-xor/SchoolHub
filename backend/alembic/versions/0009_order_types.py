"""Динамический справочник типов приказов + сид.

Revision ID: 0009_order_types
Revises: 0008_moves_children_template
Create Date: 2026-10-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_order_types"
down_revision: Union[str, None] = "0008_moves_children_template"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

SEED = [
    ("HR", "По личному составу"),
    ("STUDENTS", "По обучающимся"),
    ("EDUCATION", "По учебной деятельности"),
    ("SAFETY", "По охране труда и безопасности"),
    ("ADMIN", "Административно-хозяйственный"),
    ("OTHER", "Прочее"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if "order_types" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "order_types",
            sa.Column("code", sa.String(length=40), primary_key=True),
            sa.Column("title", sa.String(length=120), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        )
    for i, (code, title) in enumerate(SEED):
        exists = bind.execute(sa.text("SELECT 1 FROM order_types WHERE code = :c"), {"c": code}).first()
        if exists is None:
            bind.execute(
                sa.text("INSERT INTO order_types (code, title, position) VALUES (:c, :t, :p)"),
                {"c": code, "t": title, "p": i},
            )


def downgrade() -> None:
    if "order_types" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("order_types")
