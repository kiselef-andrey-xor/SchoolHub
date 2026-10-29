"""Справочник должностей + связи users↔positions + таблицы ВШК.

Revision ID: 0010_positions_vshk
Revises: 0009_order_types
Create Date: 2026-10-13

ВАЖНО (редакция 2): таблицы создаются ТОЛЬКО из metadata моделей — типы колонок
(UUID в PostgreSQL / CHAR(36) в SQLite, JSONB/JSON) гарантированно совпадают с ORM.
Первая редакция создавала таблицы с sa.CHAR(36): на PostgreSQL внешний ключ
CHAR(36) → users.id (UUID) не создаётся («foreign key constraint cannot be
implemented»), из-за чего старт backend падал. Ревизия также умеет лечить базу,
частично затронутую первой редакцией: таблицы с типом id вместо UUID удаляются
(в них не может быть полезных данных — конвертация users.position выполняется
ПОСЛЕ создания всех таблиц) и создаются заново из моделей.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_positions_vshk"
down_revision: Union[str, None] = "0009_order_types"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

SEED_POSITIONS = [
    ("Директор", "директор школы"),
    ("Заместитель директора по учебно-воспитательной работе",
     "зам по увр, зам директора по увр, замдиректора по увр, завуч"),
    ("Заместитель директора по воспитательной работе",
     "зам по вр, зам директора по вр, замдиректора по вр"),
    ("Заместитель директора по научно-методической работе",
     "зам по нмр, зам директора по нмр, замдиректора по нмр, нмр"),
    ("Заместитель директора по административно-хозяйственной работе",
     "зам по арх, зам директора по арх, замдиректора по арх"),
    ("Заместитель директора по начальной школе",
     "зам по начальной школе, замдиректора по начальной школе, зам по ноо"),
    ("Заместитель директора по безопасности", "зам по безопасности"),
    ("Заместитель директора по социальной работе",
     "зам по ср, зам директора по ср, зам по социальной работе"),
    ("Заместитель директора по информационно-коммуникационным технологиям",
     "зам по икт, зам по информатизации"),
    ("Заместитель директора по проектной деятельности",
     "зам по проектной деятельности, руководитель проектной деятельности, руководитель цпд"),
    ("Советник директора по воспитанию", "советник по воспитанию, советник директора по воспитанию"),
    ("Главный бухгалтер", "главбух"),
    ("Классный руководитель", "классные руководители, классные руководители классов"),
    ("Руководитель ШМО", "руководители шмо, председатель шмо, руководители методических объединений"),
    ("Педагог-психолог", "психолог"),
    ("Заведующий ИБЦ", "заведующий ибц, библиотекарь"),
]

# таблицы ревизии и их колонки типа GUID (в PostgreSQL обязаны быть uuid)
_GUID_COLUMNS = {
    "positions": ["id"],
    "user_positions": ["user_id", "position_id"],
    "vshk_plans": ["id", "created_by_id"],
    "vshk_items": ["id", "plan_id"],
}
# порядок удаления — обратный зависимостям (внешние ключи)
_DROP_ORDER = ("vshk_items", "user_positions", "vshk_plans", "positions")


def _guid() -> str:
    return str(uuid.uuid4())


def _pg_col_type(table: str, column: str) -> Union[str, None]:
    """data_type колонки в PostgreSQL (None, если таблицы/колонки нет)."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c LIMIT 1"
        ),
        {"t": table, "c": column},
    ).first()
    return row[0] if row is not None else None


def _has_bad_guid_types(tables: set) -> bool:
    """Признак частично применённой ПЕРВОЙ редакции ревизии (id CHAR(36) вместо UUID)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False  # в SQLite GUID и есть CHAR(36) — типы корректны
    for table, columns in _GUID_COLUMNS.items():
        if table not in tables:
            continue
        for column in columns:
            ctype = _pg_col_type(table, column)
            if ctype is not None and ctype != "uuid":
                return True
    return False


def upgrade() -> None:
    from app.models.position import Position, UserPosition
    from app.models.vshk import VshkItem, VshkPlan

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # --- лечение после частичного применения первой редакции ревизии ---
    if _has_bad_guid_types(tables):
        for table in _DROP_ORDER:
            if table in tables:
                op.drop_table(table)
        tables = set(sa.inspect(bind).get_table_names())

    # --- таблицы строго из metadata моделей (типы совпадают с ORM на любом диалекте) ---
    for model in (Position, UserPosition, VshkPlan, VshkItem):
        model.__table__.create(bind=bind, checkfirst=True)

    # --- сид справочника должностей ---
    for i, (title, aliases) in enumerate(SEED_POSITIONS, start=1):
        exists = bind.execute(sa.text("SELECT 1 FROM positions WHERE title = :t"), {"t": title}).first()
        if exists is None:
            bind.execute(
                sa.text("INSERT INTO positions (id, title, aliases, sort_order) VALUES (:id, :t, :a, :s)"),
                {"id": _guid(), "t": title, "a": aliases, "s": i},
            )

    # --- конвертация users.position → справочник + назначения + удаление колонки ---
    cols = [c["name"] for c in sa.inspect(bind).get_columns("users")]
    if "position" in cols:
        rows = bind.execute(
            sa.text("SELECT DISTINCT position FROM users WHERE position IS NOT NULL AND position <> ''")
        ).fetchall()
        for (title,) in rows:
            title = (title or "").strip()[:255]
            if not title:
                continue
            existing = bind.execute(sa.text("SELECT id FROM positions WHERE title = :t"), {"t": title}).first()
            if existing is None:
                existing = (_guid(),)
                bind.execute(
                    sa.text("INSERT INTO positions (id, title, aliases, sort_order) VALUES (:id, :t, '', 999)"),
                    {"id": existing[0], "t": title},
                )
            pos_id = existing[0]
            users = bind.execute(sa.text("SELECT id FROM users WHERE position = :t"), {"t": title}).fetchall()
            for (uid,) in users:
                link = bind.execute(
                    sa.text("SELECT 1 FROM user_positions WHERE user_id = :u AND position_id = :p"),
                    {"u": uid, "p": pos_id},
                ).first()
                if link is None:
                    bind.execute(
                        sa.text("INSERT INTO user_positions (user_id, position_id) VALUES (:u, :p)"),
                        {"u": uid, "p": pos_id},
                    )
        op.drop_column("users", "position")


def downgrade() -> None:
    bind = op.get_bind()
    op.add_column("users", sa.Column("position", sa.String(length=255), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE users SET position = t.title FROM "
            "(SELECT up.user_id AS uid, p.title AS title FROM user_positions up JOIN positions p ON p.id = up.position_id) t "
            "WHERE users.id = t.uid"
        )
        if bind.dialect.name == "postgresql"
        else sa.text("UPDATE users SET position = NULL")
    )
    for table in _DROP_ORDER:
        if table in sa.inspect(bind).get_table_names():
            op.drop_table(table)
