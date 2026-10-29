"""Грант Владельца «все служебные записки»: users.notes_full_access.

Revision ID: 0016_notes_full_access
Revises: 0015_task_occurrences
Create Date: 2026-10-29

С v1.11 журнал служебных записок по умолчанию виден каждому только в части
«свои» (автор / исполнитель / согласующий); целиком — Владельцу и сотрудникам,
которым Владелец включил этот грант в карточке (колонка notes_full_access).
У всех существующих сотрудников грант выключен (FALSE) — прежнее поведение
администрации («вижу всё») заменяется персональным разрешением Владельца.

Параллельно то же изменение делает идемпотентный шаг `_m023_notes_full_access`
в `app/migrations.py` (правило docs/DEV-GUIDE.md §4.6 — схема вносится в ОБЕ
ветки; каждая идемпотентна и проверяет состояние сама).

Тип колонки берётся ИЗ МОДЕЛИ (правило docs/DEV-GUIDE.md §4.6).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_notes_full_access"
down_revision: Union[str, None] = "0015_task_occurrences"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "notes_full_access"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table(_TABLE) or _has_column(_TABLE, _COLUMN):
        return
    from app.models.user import User

    column = User.__table__.c[_COLUMN]
    # NOT NULL + server_default FALSE: существующие строки остаются без гранта
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, column.type, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if not _has_table(_TABLE) or not _has_column(_TABLE, _COLUMN):
        return
    # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
