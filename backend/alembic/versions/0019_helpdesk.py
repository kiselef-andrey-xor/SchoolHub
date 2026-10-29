"""Helpdesk: helpdesk_tickets/messages/files + users.helpdesk_access.

Revision ID: 0019_helpdesk
Revises: 0018_substitutions_access
Create Date: 2026-10-29

Модуль заявок сотрудников в техническую поддержку школы: «зажевало принтер»,
«не включается компьютер», «нет интернета на интерактивной доске». Заявку подаёт
любой сотрудник, видит её очередь системных администраторов — сотрудников,
которым Владелец выдал грант `users.helpdesk_access` (колонка добавляется здесь же).

Роль и должность значения не имеют: сисадмином может быть учитель информатики
с ролью TEACHER. У существующих сотрудников грант выключен (FALSE), поэтому
после обновления очередь пуста, пока Владелец не назначит исполнителей.

Таблицы создаются ИЗ МЕТАДАННЫХ МОДЕЛЕЙ (правило docs/DEV-GUIDE.md §4.6):
`GUID()` в PostgreSQL есть UUID, а в SQLite CHAR(36), и внешний ключ, описанный
вручную как CHAR(36) → users.id (UUID), на PostgreSQL не создаётся вовсе
(«foreign key constraint cannot be implemented») — backend падает при старте.

Параллельно то же изменение делает идемпотентный шаг `_m026_helpdesk`
в `app/migrations.py`.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_helpdesk"
down_revision: Union[str, None] = "0018_substitutions_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("helpdesk_files", "helpdesk_messages", "helpdesk_tickets")   # порядок удаления (FK)
_GRANT = "helpdesk_access"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    from app.models.helpdesk import HelpdeskFile, HelpdeskMessage, HelpdeskTicket
    from app.models.user import User

    if _has_table("users") and not _has_column("users", _GRANT):
        # тип колонки берётся ИЗ МОДЕЛИ: ручное sa.Boolean() может разъехаться с ORM
        user_column = User.__table__.c[_GRANT]
        # NOT NULL + server_default FALSE: никто не становится сисадмином «сам собой»
        op.add_column(
            "users",
            sa.Column(_GRANT, user_column.type, nullable=False, server_default=sa.false()),
        )

    if not _has_table("users") or not _has_table("equipment_items"):
        # экзотическая база: внешние ключи не на что опереть, таблицы доберёт
        # продуктивный путь (create_all + шаг m026)
        return

    # порядок создания — от заявки к зависимым таблицам
    for model in (HelpdeskTicket, HelpdeskMessage, HelpdeskFile):
        model.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in _TABLES:
        if _has_table(table):
            op.drop_table(table)
    if _has_table("users") and _has_column("users", _GRANT):
        # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
        with op.batch_alter_table("users") as batch:
            batch.drop_column(_GRANT)
