"""Грант Владельца «кадровик»: users.substitutions_access.

Revision ID: 0018_substitutions_access
Revises: 0017_substitution_transfer
Create Date: 2026-10-29

Журнал замещений с v1.11 виден целиком Владельцу, администрации и сотруднику,
которому Владелец выдал этот грант («кадровик» — должность значения не имеет:
классный руководитель, руководитель ШМО и т.п.). Кадровик также удаляет строки
замещений. У всех существующих сотрудников грант выключен (FALSE).

Параллельно то же изменение делает идемпотентный шаг `_m025_substitutions_access`
в `app/migrations.py` (правило docs/DEV-GUIDE.md §4.6 — схема вносится в ОБЕ
ветки; каждая идемпотентна и проверяет состояние сама).

Тип колонки берётся ИЗ МОДЕЛИ (правило docs/DEV-GUIDE.md §4.6).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_substitutions_access"
down_revision: Union[str, None] = "0017_substitution_transfer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "substitutions_access"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table(_TABLE) or _has_column(_TABLE, _COLUMN):
        return
    from app.models.user import User

    column = User.__table__.c[_COLUMN]
    # NOT NULL + server_default FALSE: существующие сотрудники остаются без гранта
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, column.type, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if not _has_table(_TABLE) or not _has_column(_TABLE, _COLUMN):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
