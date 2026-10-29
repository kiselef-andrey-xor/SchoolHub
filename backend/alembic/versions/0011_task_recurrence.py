"""Повторяющиеся задачи: recurrence, recurrence_weekdays, recurrence_day, last_done_at.

Revision ID: 0011_task_recurrence
Revises: 0010_positions_vshk
Create Date: 2026-10-13

Колонки добавляются ТИПАМИ ИЗ МОДЕЛИ (Task.__table__.c[...].type) — правило
docs/DEV-GUIDE.md: типы должны гарантированно совпадать с ORM на любом диалекте
(UUID/JSONB в PostgreSQL, CHAR(36)/JSON в SQLite).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_task_recurrence"
down_revision: Union[str, None] = "0010_positions_vshk"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

_COLUMNS = ("recurrence", "recurrence_weekdays", "recurrence_day", "last_done_at")


def upgrade() -> None:
    from app.models.task import Task

    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("tasks")}
    for name in _COLUMNS:
        if name in existing:
            continue
        column = Task.__table__.c[name]
        op.add_column(
            "tasks",
            sa.Column(
                name,
                column.type,
                nullable=False if name == "recurrence" else True,
                server_default="NONE" if name == "recurrence" else None,
            ),
        )
    # на НОВОЙ базе 0001_baseline уже создал схему из metadata моделей вместе с
    # индексом — поэтому создаём его только если его ещё нет (идемпотентность)
    insp = sa.inspect(bind)
    indexes = {i["name"] for i in insp.get_indexes("tasks")}
    if "ix_tasks_recurrence" not in indexes:
        op.create_index("ix_tasks_recurrence", "tasks", ["recurrence"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("tasks")}
    if "ix_tasks_recurrence" in indexes:
        op.drop_index("ix_tasks_recurrence", table_name="tasks")
    existing = {c["name"] for c in sa.inspect(bind).get_columns("tasks")}
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("tasks", name)
