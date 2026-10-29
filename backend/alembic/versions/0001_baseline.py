"""Базовая ревизия: схема SchoolHub v1.2 (приказы, задачи с несколькими
исполнителями и вложениями, планы работы, refresh-сессии).

Для НОВЫХ баз создаёт все таблицы из metadata моделей. Существующие базы
(созданные create_all до подключения Alembic) при старте приложения
штампуются на head — см. app/database.py::init_db.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-12
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.database import Base
    import app.models  # noqa: F401 — регистрация таблиц в metadata

    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    from app.database import Base
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())
