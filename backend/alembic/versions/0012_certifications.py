"""Модуль «Аттестация и КПК»: таблица certifications.

Revision ID: 0012_certifications
Revises: 0011_task_recurrence
Create Date: 2026-10-13

Таблица создаётся СТРОГО из metadata модели (docs/DEV-GUIDE.md): типы колонок
(UUID в PostgreSQL / CHAR(36) в SQLite, даты, внешние ключи) гарантированно
совпадают с ORM, внешний ключ на users.id создаётся корректно.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_certifications"
down_revision: Union[str, None] = "0011_task_recurrence"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    from app.models.certification import Certification

    bind = op.get_bind()
    Certification.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "certifications" in sa.inspect(bind).get_table_names():
        op.drop_table("certifications")
