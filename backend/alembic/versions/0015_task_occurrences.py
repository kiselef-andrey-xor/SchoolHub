"""История повторов повторяющихся задач: task_occurrences + сводка на tasks.

Revision ID: 0015_task_occurrences
Revises: 0014_help_visibility
Create Date: 2026-10-28

Каждый ЗАКРЫТЫЙ период повторяющейся задачи теперь фиксируется строкой
task_occurrences: DONE (выполнен) или MISSED (период истёк, задачу не выполнили).
На самой задаче — денормализованная сводка для бейджа «провалено N» в списках:
occurrence_cursor (первый незакрытый период), missed_streak, last_occurrence_*.

У существующих задач occurrence_cursor остаётся NULL: отсчёт истории начинается
при первом же rollover_due «с сегодня», задним числом периоды не проваливаются.

Параллельно то же изменение делает идемпотентный шаг `_m022_task_occurrences`
в `app/migrations.py` (правило docs/DEV-GUIDE.md §4.6 — схема вносится в ОБЕ
ветки; каждая идемпотентна и проверяет состояние сама).

Типы колонок берутся ИЗ МОДЕЛИ (правило docs/DEV-GUIDE.md §4.6): ручное
`sa.String(...)` может разъехаться с ORM.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_task_occurrences"
down_revision: Union[str, None] = "0014_help_visibility"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

_TABLE = "task_occurrences"
_TASK_COLUMNS = ("occurrence_cursor", "missed_streak", "last_occurrence_start", "last_occurrence_status")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    from app.models.task import Task, TaskOccurrence

    if not _has_table(_TABLE):
        # таблица создаётся из метаданных модели: индексы и UNIQUE(task_id,
        # period_start) берутся из её определения
        TaskOccurrence.__table__.create(bind=op.get_bind(), checkfirst=True)

    if not _has_table("tasks"):
        # экзотическая база без таблицы задач: колонки сводки добавлять некуда,
        # их доберёт продуктивный путь (create_all + шаг m022)
        return

    tasks = Task.__table__
    if not _has_column("tasks", "occurrence_cursor"):
        op.add_column("tasks", sa.Column("occurrence_cursor", tasks.c.occurrence_cursor.type, nullable=True))
    if not _has_column("tasks", "missed_streak"):
        # NOT NULL + server_default: существующие строки получают 0
        op.add_column(
            "tasks",
            sa.Column("missed_streak", tasks.c.missed_streak.type, nullable=False, server_default=sa.text("0")),
        )
    if not _has_column("tasks", "last_occurrence_start"):
        op.add_column("tasks", sa.Column("last_occurrence_start", tasks.c.last_occurrence_start.type, nullable=True))
    if not _has_column("tasks", "last_occurrence_status"):
        op.add_column("tasks", sa.Column("last_occurrence_status", tasks.c.last_occurrence_status.type, nullable=True))


def downgrade() -> None:
    if _has_table("tasks"):
        # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
        with op.batch_alter_table("tasks") as batch:
            for column in reversed(_TASK_COLUMNS):
                if _has_column("tasks", column):
                    batch.drop_column(column)
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
