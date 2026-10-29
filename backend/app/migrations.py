"""Лёгкий миграционный раннер (идемпотентный, без внешних зависимостей).

Зачем: на Synology NAS развёртывание должно быть «скопировал папку → поднял контейнер»,
без обязательного прогона Alembic. Поэтому при старте приложения схема приводится
к актуальному состоянию набором версионированных шагов: каждый шаг проверяет
текущее состояние БД и применяет только отсутствующие изменения.

Для больших инсталляций рекомендуется Alembic (см. docs/DEPLOY-NAS.md → «Миграции»).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger("schoolhub.migrations")


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        row = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c LIMIT 1"
            ),
            {"t": table, "c": column},
        )
    else:  # sqlite
        rows = await conn.execute(text(f'PRAGMA table_info("{table}")'))
        return any(r[1] == column for r in rows.fetchall())
    return row.first() is not None


async def _has_index(conn: AsyncConnection, index: str) -> bool:
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        row = await conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = :i LIMIT 1"), {"i": index})
        return row.first() is not None
    rows = await conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :i"), {"i": index})
    return rows.first() is not None


Step = Callable[[AsyncConnection], Awaitable[bool]]


async def _m001_ad_columns(conn: AsyncConnection) -> bool:
    """Колонки интеграции с Active Directory в таблице users. Возвращает True, если были изменения."""
    changed = False
    columns = {
        "auth_source": "VARCHAR(16) NOT NULL DEFAULT 'local'",
        "domain_account": "VARCHAR(120)",
        "dn": "VARCHAR(512)",
        "ad_groups": "JSON" if conn.engine.dialect.name != "postgresql" else "JSONB",
        "last_ad_sync_at": "TIMESTAMP",
    }
    for column, ddl in columns.items():
        if not await _has_column(conn, "users", column):
            await conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} {ddl}"))
            logger.info("migrations: users.%s добавлена", column)
            changed = True

    if not await _has_index(conn, "ix_users_domain_account"):
        await conn.execute(text("CREATE UNIQUE INDEX ix_users_domain_account ON users (domain_account)"))
        logger.info("migrations: индекс ix_users_domain_account создан")
        changed = True
    if not await _has_index(conn, "ix_users_auth_source"):
        await conn.execute(text("CREATE INDEX ix_users_auth_source ON users (auth_source)"))
        changed = True
    return changed


async def _m002_setup_state(conn: AsyncConnection) -> bool:
    """Таблица состояния первоначальной настройки (для уже работающих инсталляций)."""
    from app.models.setup import SetupState  # noqa: F401 — регистрация таблицы в metadata

    table = SetupState.__table__
    if conn.engine.dialect.name == "sqlite":
        exists = (
            await conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='setup_state'"))
        ).first() is not None
    else:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name='setup_state' LIMIT 1")
            )
        ).first() is not None
    if exists:
        return False

    await conn.run_sync(lambda sync_conn: table.create(sync_conn, checkfirst=True))
    logger.info("migrations: создана таблица setup_state")
    return True


async def _m003_task_multiassignee(conn: AsyncConnection) -> bool:
    """v1.2: несколько исполнителей у задачи.

    1) переносим tasks.assignee_id -> task_assignees (таблицу создаёт create_all);
    2) добавляем orders.recipient_ids (выбранный круг ознакомления);
    3) убираем устаревшую колонку tasks.assignee_id.
    """
    import uuid as _uuid

    changed = False
    dialect = conn.engine.dialect.name

    if await _has_column(conn, "tasks", "assignee_id"):
        # копируем старые назначения (идемпотентно: только отсутствующие строки)
        rows = (
            await conn.execute(
                text("SELECT id, assignee_id FROM tasks WHERE assignee_id IS NOT NULL")
            )
        ).fetchall()
        for task_id, assignee_id in rows:
            exists_row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM task_assignees WHERE task_id = :t AND user_id = :u LIMIT 1"
                    ),
                    {"t": str(task_id), "u": str(assignee_id)},
                )
            ).first()
            if exists_row:
                continue
            await conn.execute(
                text(
                    "INSERT INTO task_assignees (id, task_id, user_id, done_at, created_at) "
                    "VALUES (:id, :t, :u, NULL, CURRENT_TIMESTAMP)"
                ),
                {"id": str(_uuid.uuid4()), "t": str(task_id), "u": str(assignee_id)},
            )
            changed = True
        if rows:
            logger.info("migrations: перенесено назначений tasks.assignee_id -> task_assignees: %d", len(rows))

        # отметки «выполнено» для старых завершённых задач — чтобы история выглядела целостно
        await conn.execute(
            text(
                "UPDATE task_assignees SET done_at = CURRENT_TIMESTAMP "
                "WHERE done_at IS NULL AND task_id IN (SELECT id FROM tasks WHERE status = 'DONE')"
            )
        )
        # задачи «в работе» с единственным исполнителем считаем начатыми им
        try:
            await conn.execute(text("ALTER TABLE tasks DROP COLUMN assignee_id"))
            logger.info("migrations: колонка tasks.assignee_id удалена")
            changed = True
        except Exception:  # pragma: no cover — старый SQLite без DROP COLUMN: колонка останется, но не используется
            logger.warning("migrations: не удалось удалить tasks.assignee_id — колонка оставлена (не используется)")

    if not await _has_column(conn, "orders", "recipient_ids"):
        ddl = "JSONB" if dialect == "postgresql" else "JSON"
        await conn.execute(text(f"ALTER TABLE orders ADD COLUMN recipient_ids {ddl}"))
        logger.info("migrations: orders.recipient_ids добавлена")
        changed = True

    return changed


async def _m004_local_accounts_login_avatar(conn: AsyncConnection) -> bool:
    """Локальные учётки без e-mail: логин (транслит) + аватар; e-mail становится необязательным."""
    changed = False
    dialect = conn.engine.dialect.name

    if not await _has_column(conn, "users", "login"):
        await conn.execute(text("ALTER TABLE users ADD COLUMN login VARCHAR(120)"))
        logger.info("migrations: users.login добавлена")
        changed = True
    if not await _has_index(conn, "ix_users_login"):
        await conn.execute(text("CREATE UNIQUE INDEX ix_users_login ON users (login)"))
        logger.info("migrations: индекс ix_users_login создан")
        changed = True

    if not await _has_column(conn, "users", "avatar"):
        await conn.execute(text("ALTER TABLE users ADD COLUMN avatar VARCHAR(512)"))
        logger.info("migrations: users.avatar добавлена")
        changed = True

    if dialect == "postgresql":
        row = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'email'"
            )
        )
        first = row.first()
        if first is not None and first[0] == "NO":
            await conn.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))
            logger.info("migrations: users.email теперь допускает NULL")
            changed = True
    else:
        # SQLite (dev/тесты): базы создаются заново из metadata, где email уже nullable.
        # In-place DROP NOT NULL в SQLite не поддерживается — осознанно пропускаем.
        pass
    return changed


async def _m005_user_deleted_must_change(conn: AsyncConnection) -> bool:
    """users.is_deleted (удаление с сохранением истории) + users.password_must_change."""
    changed = False
    for column, ddl in (
        ("is_deleted", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("password_must_change", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ):
        if not await _has_column(conn, "users", column):
            await conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} {ddl}"))
            logger.info("migrations: users.%s добавлена", column)
            changed = True
    if not await _has_index(conn, "ix_users_is_deleted"):
        await conn.execute(text("CREATE INDEX ix_users_is_deleted ON users (is_deleted)"))
        changed = True
    return changed


async def _m006_incoming_journal(conn: AsyncConnection) -> bool:
    """Таблица журнала входящих писем + users.incoming_journal_access + tasks.incoming_number."""
    from app.models.incoming import IncomingLetter  # noqa: F401

    changed = False
    if not await _has_column(conn, "users", "incoming_journal_access"):
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN incoming_journal_access BOOLEAN NOT NULL DEFAULT FALSE")
        )
        changed = True
    if not await _has_column(conn, "tasks", "incoming_number"):
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN incoming_number VARCHAR(120)"))
        changed = True
    dialect = conn.engine.dialect.name
    table = IncomingLetter.__table__
    existing = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if "incoming_letters" not in existing:
        await conn.run_sync(lambda sc: table.create(sc))
        changed = True
    else:
        for column in ("reg_year", "reg_no", "sent_date", "addressee", "note", "responsible_id", "task_id"):
            if not await _has_column(conn, "incoming_letters", column):
                ddl = {
                    "reg_year": "INTEGER NOT NULL",
                    "reg_no": "INTEGER NOT NULL",
                    "sent_date": "DATE",
                    "addressee": "VARCHAR(255)",
                    "note": "VARCHAR(1000)",
                    "responsible_id": "CHAR(32)" if dialect != "postgresql" else "UUID",
                    "task_id": "CHAR(32)" if dialect != "postgresql" else "UUID",
                }[column]
                await conn.execute(text(f"ALTER TABLE incoming_letters ADD COLUMN {column} {ddl}"))
                changed = True
    return changed


async def _m007_movement_and_audit_name(conn: AsyncConnection) -> bool:
    """Раздел «Движение»: таблица movement_orders, users.movement_access, audit_log.actor_name."""
    from app.models.movement import MovementOrder  # noqa: F401

    changed = False
    if not await _has_column(conn, "users", "movement_access"):
        await conn.execute(text("ALTER TABLE users ADD COLUMN movement_access BOOLEAN NOT NULL DEFAULT FALSE"))
        changed = True
    if not await _has_column(conn, "audit_log", "actor_name"):
        await conn.execute(text("ALTER TABLE audit_log ADD COLUMN actor_name VARCHAR(255)"))
        changed = True
    existing = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if "movement_orders" not in existing:
        await conn.run_sync(lambda sc: MovementOrder.__table__.create(sc))
        changed = True
    return changed


async def _m008_movement_print_text(conn: AsyncConnection) -> bool:
    """movement_orders.print_text — редактируемая печатная форма приказа."""
    if not await _has_column(conn, "movement_orders", "print_text"):
        ddl = "TEXT" if conn.engine.dialect.name != "postgresql" else "TEXT"
        await conn.execute(text(f"ALTER TABLE movement_orders ADD COLUMN print_text {ddl}"))
        return True
    return False


async def _m009_equipment(conn: AsyncConnection) -> bool:
    """Учёт оборудования: таблица equipment_items + users.equipment_access."""
    from app.models.equipment import EquipmentItem  # noqa: F401

    changed = False
    if not await _has_column(conn, "users", "equipment_access"):
        await conn.execute(text("ALTER TABLE users ADD COLUMN equipment_access BOOLEAN NOT NULL DEFAULT FALSE"))
        changed = True
    existing = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if "equipment_items" not in existing:
        await conn.run_sync(lambda sc: EquipmentItem.__table__.create(sc))
        changed = True
    return changed


async def _m010_moves_children_template(conn: AsyncConnection) -> bool:
    """Перемещения инвентаря, дети в приказах движения, глобальный шаблон печати."""
    from app.models.equipment import EquipmentMove  # noqa: F401
    from app.models.movement import MovementChild  # noqa: F401

    changed = False
    if not await _has_column(conn, "setup_state", "movement_template"):
        await conn.execute(text("ALTER TABLE setup_state ADD COLUMN movement_template TEXT"))
        changed = True
    existing = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if "movement_children" not in existing:
        await conn.run_sync(lambda sc: MovementChild.__table__.create(sc))
        changed = True
    if "equipment_moves" not in existing:
        await conn.run_sync(lambda sc: EquipmentMove.__table__.create(sc))
        changed = True
    if conn.engine.dialect.name == "postgresql":
        row = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'movement_orders' AND column_name = 'guardian'"
            )
        )
        first = row.first()
        if first is not None and first[0] == "NO":
            await conn.execute(text("ALTER TABLE movement_orders ALTER COLUMN guardian DROP NOT NULL"))
            changed = True
        row = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'movement_orders' AND column_name = 'birth_date'"
            )
        )
        first = row.first()
        if first is not None and first[0] == "NO":
            await conn.execute(text("ALTER TABLE movement_orders ALTER COLUMN birth_date DROP NOT NULL"))
            changed = True
    # backfill: приказам, созданным до появления children, добавляем ребёнка из старых колонок
    import uuid as _uuid

    legacy = (
        await conn.execute(
            text("SELECT id, student, birth_date, from_place, to_place FROM movement_orders")
        )
    ).all()
    for row in legacy:
        cnt = (
            await conn.execute(
                text("SELECT COUNT(*) FROM movement_children WHERE movement_id = :mid"), {"mid": str(row[0])}
            )
        ).scalar_one()
        if not cnt and row[1]:
            await conn.execute(
                text(
                    "INSERT INTO movement_children (id, movement_id, student, birth_date, from_place, to_place, position) "
                    "VALUES (:id, :mid, :student, :birth, :frm, :to, 0)"
                ),
                {
                    "id": str(_uuid.uuid4()), "mid": str(row[0]), "student": row[1],
                    "birth": row[2], "frm": row[3], "to": row[4],
                },
            )
            changed = True
    return changed


SEED_ORDER_TYPES = [
    ("HR", "По личному составу"),
    ("STUDENTS", "По обучающимся"),
    ("EDUCATION", "По учебной деятельности"),
    ("SAFETY", "По охране труда и безопасности"),
    ("ADMIN", "Административно-хозяйственный"),
    ("OTHER", "Прочее"),
]


async def _m011_order_types(conn: AsyncConnection) -> bool:
    """Динамический справочник типов приказов + сид текущих значений."""
    from app.models.order_type import OrderTypeRow  # noqa: F401

    changed = False
    existing = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if "order_types" not in existing:
        await conn.run_sync(lambda sc: OrderTypeRow.__table__.create(sc))
        changed = True
    for i, (code, title) in enumerate(SEED_ORDER_TYPES):
        row = (await conn.execute(text("SELECT 1 FROM order_types WHERE code = :c"), {"c": code})).first()
        if row is None:
            await conn.execute(
                text("INSERT INTO order_types (code, title, position) VALUES (:c, :t, :p)"),
                {"c": code, "t": title, "p": i},
            )
            changed = True
    return changed


async def _m012_positions_vshk(conn: AsyncConnection) -> bool:
    """Справочник должностей + связи users↔positions + таблицы ВШК.

    Свободное текстовое поле users.position конвертируется в записи справочника
    (каждое уникальное значение → должность + назначение), после чего колонка удаляется.
    """
    import uuid as _uuid

    from app.models.position import Position, UserPosition  # noqa: F401
    from app.models.vshk import VshkItem, VshkPlan  # noqa: F401
    from app.services.positions import SEED_POSITIONS, normalize

    changed = False
    tables = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    for model in (Position, UserPosition, VshkPlan, VshkItem):
        if model.__table__.name not in tables:
            await conn.run_sync(lambda sc, m=model: m.__table__.create(sc))
            changed = True

    # --- сид справочника должностей ---
    for i, (title, aliases) in enumerate(SEED_POSITIONS, start=1):
        row = (await conn.execute(text("SELECT 1 FROM positions WHERE title = :t"), {"t": title})).first()
        if row is None:
            await conn.execute(
                text("INSERT INTO positions (id, title, aliases, sort_order) VALUES (:id, :t, :a, :s)"),
                {"id": str(_uuid.uuid4()), "t": title, "a": aliases, "s": i},
            )
            changed = True

    # --- конвертация users.position → справочник + назначения ---
    if await _has_column(conn, "users", "position"):
        rows = (
            await conn.execute(
                text("SELECT DISTINCT position FROM users WHERE position IS NOT NULL AND position <> ''")
            )
        ).fetchall()
        for (title,) in rows:
            title = (title or "").strip()[:255]
            if not title:
                continue
            existing = (
                await conn.execute(text("SELECT id FROM positions WHERE title = :t"), {"t": title})
            ).first()
            if existing is None:
                # совпадение без учёта регистра/знаков — не дублим синонимичные названия
                all_rows = (await conn.execute(text("SELECT id, title FROM positions"))).fetchall()
                match = next((r for r in all_rows if normalize(r[1]) == normalize(title)), None)
                if match is not None:
                    existing = match
                else:
                    new_id = str(_uuid.uuid4())
                    await conn.execute(
                        text("INSERT INTO positions (id, title, aliases, sort_order) VALUES (:id, :t, '', 999)"),
                        {"id": new_id, "t": title},
                    )
                    existing = (new_id,)
            pos_id = existing[0]
            users = (
                await conn.execute(
                    text("SELECT id FROM users WHERE position = :t"), {"t": title}
                )
            ).fetchall()
            for (uid,) in users:
                link = (
                    await conn.execute(
                        text("SELECT 1 FROM user_positions WHERE user_id = :u AND position_id = :p"),
                        {"u": uid, "p": pos_id},
                    )
                ).first()
                if link is None:
                    await conn.execute(
                        text("INSERT INTO user_positions (user_id, position_id) VALUES (:u, :p)"),
                        {"u": uid, "p": pos_id},
                    )
                    changed = True
        dialect = conn.engine.dialect.name
        if dialect == "postgresql":
            await conn.execute(text("ALTER TABLE users DROP COLUMN position"))
        else:
            try:
                await conn.execute(text("ALTER TABLE users DROP COLUMN position"))
            except Exception:  # pragma: no cover — очень старый SQLite оставит колонку-призрак
                logger.warning("SQLite не поддержал DROP COLUMN users.position — колонка останется пустой")
        changed = True
    return changed


async def _m013_task_recurrence(conn: AsyncConnection) -> bool:
    """Повторяющиеся задачи: колонки recurrence/recurrence_weekdays/recurrence_day/last_done_at."""
    changed = False
    columns = {
        "recurrence": "VARCHAR(10) NOT NULL DEFAULT 'NONE'",
        "recurrence_weekdays": "JSON" if conn.engine.dialect.name != "postgresql" else "JSONB",
        "recurrence_day": "INTEGER",
        "last_done_at": "TIMESTAMP",
    }
    for column, ddl in columns.items():
        if not await _has_column(conn, "tasks", column):
            await conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}"))
            logger.info("migrations: tasks.%s добавлена", column)
            changed = True

    if not await _has_index(conn, "ix_tasks_recurrence"):
        await conn.execute(text("CREATE INDEX ix_tasks_recurrence ON tasks (recurrence)"))
        logger.info("migrations: индекс ix_tasks_recurrence создан")
        changed = True
    return changed


async def _m014_certifications(conn: AsyncConnection) -> bool:
    """Таблица «Аттестация и КПК» (квалификационные категории и курсы повышения)."""
    from app.models.certification import Certification

    changed = False
    tables = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if Certification.__table__.name not in tables:
        await conn.run_sync(lambda sc: Certification.__table__.create(sc))
        changed = True
    if not await _has_index(conn, "ix_certifications_expires_at"):
        await conn.execute(text("CREATE INDEX ix_certifications_expires_at ON certifications (expires_at)"))
        changed = True
    return changed


async def _m016_user_birth_date(conn: AsyncConnection) -> bool:
    """Дата рождения сотрудника (необязательное поле карточки, виджет «Дни рождения»)."""
    if not await _has_column(conn, "users", "birth_date"):
        await conn.execute(text("ALTER TABLE users ADD COLUMN birth_date DATE"))
        logger.info("migrations: users.birth_date добавлена")
        return True
    return False


async def _m018_system_flags(conn: AsyncConnection) -> bool:
    """Таблица системных флагов рантайма (режим «Локдаун»)."""
    from app.models.system_flag import SystemFlag

    tables = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if SystemFlag.__table__.name not in tables:
        await conn.run_sync(lambda sc: SystemFlag.__table__.create(sc))
        logger.info("migrations: таблица system_flags создана")
        return True
    return False


async def _m017_user_list_views(conn: AsyncConnection) -> bool:
    """Таблица закрепляемых видов списков («Запомнить этот вид» в фильтрах)."""
    from app.models.list_view import UserListView

    tables = await conn.run_sync(lambda sc: set(__import__("sqlalchemy").inspect(sc).get_table_names()))
    if UserListView.__table__.name not in tables:
        await conn.run_sync(lambda sc: UserListView.__table__.create(sc))
        logger.info("migrations: таблица user_list_views создана")
        return True
    return False


async def _m019_totp_pep(conn: AsyncConnection) -> bool:
    """ПЭП через аутентификатор (TOTP) + политика подписи приказов.

    users: totp_secret_enc / totp_enabled / totp_confirmed_at / recovery_word_set_at;
    orders: signature_policy; order_acknowledgements: sign_method.
    """
    changed = False
    for col, ddl in (
        ("totp_secret_enc", "ALTER TABLE users ADD COLUMN totp_secret_enc VARCHAR(512)"),
        ("totp_confirmed_at", "ALTER TABLE users ADD COLUMN totp_confirmed_at TIMESTAMP"),
        ("recovery_word_set_at", "ALTER TABLE users ADD COLUMN recovery_word_set_at TIMESTAMP"),
    ):
        if not await _has_column(conn, "users", col):
            await conn.execute(text(ddl))
            logger.info("migrations: users.%s добавлена", col)
            changed = True
    if not await _has_column(conn, "users", "totp_enabled"):
        # SQLite не принимает NOT NULL без DEFAULT в ALTER — пишем совместимо с обоими диалектами
        await conn.execute(text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0"))
        logger.info("migrations: users.totp_enabled добавлена")
        changed = True
    if not await _has_column(conn, "orders", "signature_policy"):
        await conn.execute(text("ALTER TABLE orders ADD COLUMN signature_policy VARCHAR(20) NOT NULL DEFAULT 'CLICK'"))
        logger.info("migrations: orders.signature_policy добавлена")
        changed = True
    if not await _has_column(conn, "order_acknowledgements", "sign_method"):
        await conn.execute(text("ALTER TABLE order_acknowledgements ADD COLUMN sign_method VARCHAR(20)"))
        logger.info("migrations: order_acknowledgements.sign_method добавлена")
        changed = True
    return changed


async def _m015_equipment_barcode(conn: AsyncConnection) -> bool:
    """Штрихкод позиции инвентаря (сканирование этикеток EAN-13/Code-128)."""
    changed = False
    if not await _has_column(conn, "equipment_items", "barcode"):
        await conn.execute(text("ALTER TABLE equipment_items ADD COLUMN barcode VARCHAR(128)"))
        logger.info("migrations: equipment_items.barcode добавлена")
        changed = True
    if not await _has_index(conn, "ix_equipment_items_barcode"):
        await conn.execute(text("CREATE INDEX ix_equipment_items_barcode ON equipment_items (barcode)"))
        logger.info("migrations: индекс ix_equipment_items_barcode создан")
        changed = True
    return changed


async def _m020_journal_modules(conn: AsyncConnection) -> bool:
    """Модули 2–4 ТЗ: служебные записки, замещения, справка/FAQ.

    Таблицы создаются из метаданных моделей (идемпотентно через checkfirst);
    индексы и уникальные ограничения берутся из определений моделей.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.help import HelpAck, HelpArticle
    from app.models.service_note import ServiceNote, ServiceNoteFile
    from app.models.substitution import Substitution  # noqa: F401

    changed = False
    for model in (ServiceNote, ServiceNoteFile, Substitution, HelpArticle, HelpAck):
        table = model.__table__
        exists = await conn.run_sync(lambda sc, t=table: t.name in set(sa_inspect(sc).get_table_names()))
        if not exists:
            await conn.run_sync(lambda sc, t=table: t.create(sc, checkfirst=True))
            logger.info("migrations: таблица %s создана", table.name)
            changed = True
    return changed


async def _m021_help_visibility(conn: AsyncConnection) -> bool:
    """Видимость статей справки/FAQ: ALL (всем) / ADMIN (администрации) / OWNER (Владельцу).

    Существующие статьи остаются общедоступными ('ALL') — прежнее поведение не меняется.
    """
    changed = False
    if not await _has_column(conn, "help_articles", "visibility"):
        # SQLite не принимает NOT NULL без DEFAULT в ALTER; значение совместимо с обоими диалектами
        await conn.execute(text(
            "ALTER TABLE help_articles ADD COLUMN visibility VARCHAR(10) NOT NULL DEFAULT 'ALL'"
        ))
        logger.info("migrations: help_articles.visibility добавлена")
        changed = True
    if not await _has_index(conn, "ix_help_articles_visibility"):
        await conn.execute(text(
            "CREATE INDEX ix_help_articles_visibility ON help_articles (visibility)"
        ))
        logger.info("migrations: индекс ix_help_articles_visibility создан")
        changed = True
    return changed


async def _m022_task_occurrences(conn: AsyncConnection) -> bool:
    """История повторов повторяющихся задач (v1.10): task_occurrences + сводка на tasks.

    Таблица создаётся из метаданных модели (идемпотентно через checkfirst);
    индексы и UNIQUE(task_id, period_start) берутся из определений модели.
    У существующих задач occurrence_cursor остаётся NULL — отсчёт истории
    начнётся при первом rollover_due «с сегодня» (задним числом не проваливаем).
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.task import TaskOccurrence

    changed = False
    table = TaskOccurrence.__table__
    exists = await conn.run_sync(lambda sc: table.name in set(sa_inspect(sc).get_table_names()))
    if not exists:
        await conn.run_sync(lambda sc: table.create(sc, checkfirst=True))
        logger.info("migrations: таблица task_occurrences создана")
        changed = True
    if not await _has_column(conn, "tasks", "occurrence_cursor"):
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN occurrence_cursor DATE"))
        changed = True
    if not await _has_column(conn, "tasks", "missed_streak"):
        # SQLite не принимает NOT NULL без DEFAULT в ALTER; 0 совместимо с обоими диалектами
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN missed_streak INTEGER NOT NULL DEFAULT 0"))
        changed = True
    if not await _has_column(conn, "tasks", "last_occurrence_start"):
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN last_occurrence_start DATE"))
        changed = True
    if not await _has_column(conn, "tasks", "last_occurrence_status"):
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN last_occurrence_status VARCHAR(8)"))
        changed = True
    if changed:
        logger.info("migrations: m022 история повторов задач применена")
    return changed


async def _m023_notes_full_access(conn: AsyncConnection) -> bool:
    """Грант Владельца «все служебные записки»: users.notes_full_access (v1.11).

    Существующие сотрудники остаются без гранта (FALSE): журнал записок с 1.11
    виден каждому только в части «свои», целиком — Владельцу и гранту.
    """
    changed = False
    if not await _has_column(conn, "users", "notes_full_access"):
        # SQLite не принимает NOT NULL без DEFAULT в ALTER; FALSE совместим с обоими диалектами
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN notes_full_access BOOLEAN NOT NULL DEFAULT 0"
        ))
        logger.info("migrations: users.notes_full_access добавлена")
        changed = True
    return changed


async def _m024_substitution_transfer(conn: AsyncConnection) -> bool:
    """Замещения: substitutions.transferred_at — момент фактической передачи объектов (v1.11).

    С v1.11 задачи/планы/ВШК передаются замещающему НЕ при создании, а после его
    подтверждения, и возвращаются отсутствующему при отмене, завершении или удалении
    строки. Колонка хранит момент передачи (NULL — не передано или уже возвращено).

    Заполнение старых строк выполняется ОДНОВРЕМЕННО с добавлением колонки: сводка
    прежних версий («передано задач: N») означала, что передача уже состоялась.
    Повторно на каждом старте UPDATE не гоняем — иначе строки с возвратом
    («возвращено задач: N») снова получили бы признак передачи.
    """
    if await _has_column(conn, "substitutions", "transferred_at"):
        return False
    await conn.execute(text("ALTER TABLE substitutions ADD COLUMN transferred_at TIMESTAMP"))
    await conn.execute(text(
        "UPDATE substitutions SET transferred_at = COALESCE(confirmed_at, created_at) "
        "WHERE transferred_at IS NULL AND (note LIKE '%передано%' OR note LIKE '%переписано%') "
        "AND note NOT LIKE '%возвращено%'"
    ))
    logger.info("migrations: substitutions.transferred_at добавлена (старые строки заполнены по сводке)")
    return True


async def _m025_substitutions_access(conn: AsyncConnection) -> bool:
    """Грант Владельца «кадровик»: users.substitutions_access (v1.11).

    Журнал замещений целиком + удаление строк замещений. Существующие сотрудники
    остаются без гранта (FALSE).
    """
    if await _has_column(conn, "users", "substitutions_access"):
        return False
    # SQLite не принимает NOT NULL без DEFAULT в ALTER; FALSE совместим с обоими диалектами
    await conn.execute(text(
        "ALTER TABLE users ADD COLUMN substitutions_access BOOLEAN NOT NULL DEFAULT 0"
    ))
    logger.info("migrations: users.substitutions_access добавлена")
    return True


async def _m026_helpdesk(conn: AsyncConnection) -> bool:
    """Helpdesk (v1.12): заявки сотрудников в техподдержку + грант «системный администратор».

    Таблицы создаются из метаданных моделей (идемпотентно через checkfirst):
    индексы и внешние ключи берутся из определений моделей, поэтому GUID остаётся
    GUID на обоих диалектах (правило docs/DEV-GUIDE.md §4.6 — не писать типы вручную).
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.helpdesk import HelpdeskFile, HelpdeskMessage, HelpdeskTicket  # noqa: F401

    changed = False
    # грант Владельца: кто видит очередь заявок и работает с ней.
    # Существующие сотрудники остаются без гранта (FALSE) — очередь пуста до назначения.
    if not await _has_column(conn, "users", "helpdesk_access"):
        # SQLite не принимает NOT NULL без DEFAULT в ALTER; FALSE совместим с обоими диалектами
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN helpdesk_access BOOLEAN NOT NULL DEFAULT 0"
        ))
        logger.info("migrations: users.helpdesk_access добавлена")
        changed = True

    for model in (HelpdeskTicket, HelpdeskMessage, HelpdeskFile):
        table = model.__table__
        exists = await conn.run_sync(lambda sc, t=table: t.name in set(sa_inspect(sc).get_table_names()))
        if not exists:
            await conn.run_sync(lambda sc, t=table: t.create(sc, checkfirst=True))
            logger.info("migrations: таблица %s создана", table.name)
            changed = True
    return changed


async def _m027_document_pdf(conn: AsyncConnection) -> bool:
    """Автоматическая PDF-копия документа и несколько вложений у инструкции (v1.12).

    Три изменения одной ревизией — они про один сценарий «документ открывается
    кликом в новой вкладке»:

      1. колонки `pdf_*` у `service_note_files` и `task_files`: копия, её размер,
         состояние и причина неудачи. Старые строки получают PENDING — копия
         будет доделана при первом открытии документа;
      2. таблица `help_article_files`: у инструкции теперь НЕСКОЛЬКО документов
         (раньше один файл лежал прямо в `help_articles.file_*`);
      3. перенос содержимого старых колонок `help_articles.file_*` в новую
         таблицу. Колонки НЕ удаляются: их читают старые выгрузки и резервные
         копии, а удаление колонки на боевой базе — операция необратимая.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.help import HelpArticleFile  # noqa: F401

    changed = False
    pdf_columns = {
        "pdf_path": "VARCHAR(1024)",
        "pdf_size": "INTEGER",
        # NOT NULL + DEFAULT: старые строки получают PENDING и доделываются при первом просмотре
        "pdf_status": "VARCHAR(10) NOT NULL DEFAULT 'PENDING'",
        "pdf_note": "VARCHAR(300)",
    }
    for table in ("service_note_files", "task_files"):
        for column, ddl in pdf_columns.items():
            if not await _has_column(conn, table, column):
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                logger.info("migrations: %s.%s добавлена", table, column)
                changed = True

    art_files = HelpArticleFile.__table__
    exists = await conn.run_sync(lambda sc: art_files.name in set(sa_inspect(sc).get_table_names()))
    if not exists:
        await conn.run_sync(lambda sc: art_files.create(sc, checkfirst=True))
        logger.info("migrations: таблица help_article_files создана")
        changed = True

    # перенос единственного файла инструкции в новую таблицу.
    # Условие NOT EXISTS — защита от повторного запуска: шаг вызывается КАЖДЫМ
    # стартом контейнера, и без него файл дублировался бы при каждом рестарте.
    if await _has_column(conn, "help_articles", "file_path"):
        if conn.engine.dialect.name == "sqlite":
            # gen_random_uuid() в SQLite нет — собираем UUID v4 из randomblob()
            uuid_sql = (
                "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
                "substr(lower(hex(randomblob(2))), 2) || '-' || "
                "substr('89ab', abs(random()) % 4 + 1, 1) || "
                "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
            )
            now_sql = "COALESCE(h.updated_at, h.created_at, CURRENT_TIMESTAMP)"
        else:
            uuid_sql = "gen_random_uuid()"          # PostgreSQL 13+ — без pgcrypto
            now_sql = "COALESCE(h.updated_at, h.created_at, now())"
        result = await conn.execute(text(f"""
            INSERT INTO help_article_files
                (id, article_id, storage_path, original_name, mime_type, size_bytes,
                 caption, pdf_status, created_at)
            SELECT {uuid_sql}, h.id, h.file_path,
                   COALESCE(h.file_name, 'instruction'),
                   COALESCE(h.file_mime, 'application/octet-stream'),
                   COALESCE(h.file_size, 0),
                   NULL, 'PENDING', {now_sql}
            FROM help_articles h
            WHERE h.file_path IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM help_article_files f WHERE f.article_id = h.id)
        """))
        moved = result.rowcount or 0
        if moved:
            logger.info("migrations: перенесено файлов инструкций в help_article_files: %s", moved)
            changed = True
    return changed


MIGRATIONS: dict[str, Step] = {
    "m001_ad_columns": _m001_ad_columns,
    "m002_setup_state": _m002_setup_state,
    "m003_task_multiassignee": _m003_task_multiassignee,
    "m004_local_accounts_login_avatar": _m004_local_accounts_login_avatar,
    "m005_user_deleted_must_change": _m005_user_deleted_must_change,
    "m006_incoming_journal": _m006_incoming_journal,
    "m007_movement_and_audit_name": _m007_movement_and_audit_name,
    "m008_movement_print_text": _m008_movement_print_text,
    "m009_equipment": _m009_equipment,
    "m010_moves_children_template": _m010_moves_children_template,
    "m011_order_types": _m011_order_types,
    "m012_positions_vshk": _m012_positions_vshk,
    "m013_task_recurrence": _m013_task_recurrence,
    "m014_certifications": _m014_certifications,
    "m015_equipment_barcode": _m015_equipment_barcode,
    "m016_user_birth_date": _m016_user_birth_date,
    "m017_user_list_views": _m017_user_list_views,
    "m018_system_flags": _m018_system_flags,
    "m019_totp_pep": _m019_totp_pep,
    "m020_journal_modules": _m020_journal_modules,
    "m021_help_visibility": _m021_help_visibility,
    "m022_task_occurrences": _m022_task_occurrences,
    "m023_notes_full_access": _m023_notes_full_access,
    "m024_substitution_transfer": _m024_substitution_transfer,
    "m025_substitutions_access": _m025_substitutions_access,
    "m026_helpdesk": _m026_helpdesk,
    "m027_document_pdf": _m027_document_pdf,
}


async def run_migrations(conn: AsyncConnection) -> list[str]:
    """Применяет все шаги. Возвращает список шагов, которые ФАКТИЧЕСКИ что-то изменили.

    Каждый шаг идемпотентен: сам проверяет текущее состояние схемы, поэтому
    повторный запуск (каждый старт контейнера) безопасен и ничего не ломает.
    """
    applied: list[str] = []
    for name, step in MIGRATIONS.items():
        if await step(conn):
            applied.append(name)
    return applied
