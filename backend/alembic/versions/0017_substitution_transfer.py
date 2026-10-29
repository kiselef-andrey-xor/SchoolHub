"""Замещения: момент фактической передачи объектов — substitutions.transferred_at.

Revision ID: 0017_substitution_transfer
Revises: 0016_notes_full_access
Create Date: 2026-10-29

С v1.11 объекты (задачи, планы, ВШК) передаются замещающему НЕ при создании
замещения, а после его подтверждения; при отмене, завершении и удалении строки
переданное возвращается отсутствующему. Колонка `transferred_at` хранит момент
передачи и является признаком «объекты сейчас у замещающего» (NULL — не передавались
или уже возвращены).

Старые строки заполняются эвристикой по сводке в `note` («передано задач…»,
«планов переписано…») — в прежних версиях передача выполнялась при создании, так
что возврат работает и для замещений, оформленных до обновления.

Параллельно то же делает идемпотентный шаг `_m024_substitution_transfer`
в `app/migrations.py` (правило docs/DEV-GUIDE.md §4.6 — схема вносится в ОБЕ
ветки; каждая идемпотентна и проверяет состояние сама).

Тип колонки берётся ИЗ МОДЕЛИ (правило docs/DEV-GUIDE.md §4.6).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_substitution_transfer"
down_revision: Union[str, None] = "0016_notes_full_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "substitutions"
_COLUMN = "transferred_at"

# сводка передачи в прежних версиях писалась в note при создании замещения
_BACKFILL = (
    f"UPDATE {_TABLE} SET {_COLUMN} = COALESCE(confirmed_at, created_at) "
    f"WHERE {_COLUMN} IS NULL AND (note LIKE '%передано%' OR note LIKE '%переписано%') "
    f"AND note NOT LIKE '%возвращено%'"
)


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        from app.models.substitution import Substitution

        column = Substitution.__table__.c[_COLUMN]
        op.add_column(_TABLE, sa.Column(_COLUMN, column.type, nullable=True))
    op.execute(_BACKFILL)


def downgrade() -> None:
    if not _has_table(_TABLE) or not _has_column(_TABLE, _COLUMN):
        return
    # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
