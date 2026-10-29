"""Видимость статей справки/FAQ: help_articles.visibility.

Revision ID: 0014_help_visibility
Revises: 0013_totp_pep
Create Date: 2026-10-28

Колонка `visibility` (ALL | ADMIN | OWNER) ограничивает круг читателей статьи
справки/инструкции (docs/ARCHITECTURE.md, ADR-13). Существующие статьи остаются
общедоступными ('ALL') — прежнее поведение не меняется.

Параллельно то же изменение делает идемпотентный шаг `_m021_help_visibility`
в `app/migrations.py`: по правилу docs/DEV-GUIDE.md §4.6 изменение схемы вносится
в ОБЕ ветки. `app.database.init_db()` всегда прогоняет шаги после `alembic upgrade
head`, поэтому на боевых базах колонка появляется независимо от того, какой путь
сработал первым; каждый из них идемпотентен и проверяет состояние схемы сам.

Тип колонки берётся ИЗ МОДЕЛИ (правило docs/DEV-GUIDE.md §4.6): enum хранится
как строка, и ручное `sa.String(...)` может разъехаться с ORM.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_help_visibility"
down_revision: Union[str, None] = "0013_totp_pep"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

_TABLE = "help_articles"
_COLUMN = "visibility"
_INDEX = "ix_help_articles_visibility"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _has_index(index: str) -> bool:
    return index in {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    # таблицы может не быть, если база легаси и её добирает шаг m020 из metadata моделей
    if not _has_table(_TABLE):
        from app.models.help import HelpArticle

        HelpArticle.__table__.create(bind=op.get_bind(), checkfirst=True)
        return

    if not _has_column(_TABLE, _COLUMN):
        from app.models.help import HelpArticle

        column = HelpArticle.__table__.c[_COLUMN]
        # NOT NULL + server_default: существующие строки получают 'ALL'
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, column.type, nullable=False, server_default=sa.text("'ALL'")),
        )
    if not _has_index(_INDEX):
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
    with op.batch_alter_table(_TABLE) as batch:
        if _has_index(_INDEX):
            batch.drop_index(_INDEX)
        if _has_column(_TABLE, _COLUMN):
            batch.drop_column(_COLUMN)
