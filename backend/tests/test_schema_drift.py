"""Сверка схемы БД: модели ↔ фактический путь её создания (review/REVIEW.md §5.1, §5.7 п.6).

В проекте три механизма управления схемой, и это осознанный долг (§5.1):
  1. `Base.metadata.create_all()` — при старте создаёт недостающие ТАБЛИЦЫ;
  2. `app/migrations.py::run_migrations()` — идемпотентные шаги: добавляет КОЛОНКИ
     и индексы к уже существующим таблицам (create_all этого не умеет);
  3. `alembic/` — отдельная цепочка ревизий, которая отстаёт (в ней нет таблиц
     модулей 2–4: их создаёт шаг m020).

Продуктивный путь — (1) + (2). Тесты ниже фиксируют два инварианта, нарушение
которых означает «обновление на NAS сломает данные или не добавит поле»:

  * после create_all + run_migrations схема ТОЧНО соответствует моделям
    (нет ни лишних таблиц/колонок, ни недостающих) — это ловит новую колонку
    в модели без шага в migrations.py;
  * run_migrations действительно ЛЕЧИТ старую базу: если колонки нет, она
    появляется, а существующие строки получают значение по умолчанию;
  * цепочка alembic применяется к чистой базе ( guards §5.2: alembic.ini читается
    configparser'ом в кодировке локали, и при ASCII-локали падал весь старт).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import models as _all_models  # noqa: F401  — регистрация всех таблиц в metadata
from app.database import Base
from app.migrations import MIGRATIONS, run_migrations

# asyncio_mode = auto (pytest.ini): метка не нужна, а с ней синхронные тесты
# этого файла дают предупреждение pytest-asyncio.


def _reflect(sync_url: str) -> dict[str, dict[str, bool]]:
    """{table: {column: nullable}} из реальной БД."""
    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        return {
            table: {c["name"]: bool(c["nullable"]) for c in insp.get_columns(table)}
            for table in insp.get_table_names()
        }
    finally:
        engine.dispose()


def _model_schema() -> dict[str, dict[str, bool]]:
    """{table: {column: nullable}} из метаданных моделей."""
    return {
        table.name: {c.name: bool(c.nullable) for c in table.columns}
        for table in Base.metadata.sorted_tables
    }


# служебные таблицы самих механизмов миграций — их в моделях нет по определению
_SERVICE_TABLES = {"alembic_version"}


async def _build_schema(db_path: Path) -> None:
    """Повторяет продуктивный путь `app.database.init_db()`: create_all + run_migrations.

    Отдельный файл БД (не общая in-memory из фикстур): отражение схемы sync-движком
    должно увидеть ровно то, что создал async-движок.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await run_migrations(conn)
    finally:
        await engine.dispose()


async def test_create_all_plus_migrations_matches_models(tmp_path):
    """Продуктивный путь создания схемы сходится с моделями — без дрейфа."""
    db_path = tmp_path / "fresh.db"
    await _build_schema(db_path)

    actual = _reflect(f"sqlite:///{db_path}")
    expected = _model_schema()

    extra_tables = set(actual) - set(expected) - _SERVICE_TABLES
    missing_tables = set(expected) - set(actual)
    assert not missing_tables, f"в БД нет таблиц из моделей: {sorted(missing_tables)}"
    assert not extra_tables, f"в БД появились таблицы, которых нет в моделях: {sorted(extra_tables)}"

    problems: list[str] = []
    for table, columns in expected.items():
        real = actual.get(table, {})
        for column, nullable in columns.items():
            if column not in real:
                problems.append(f"{table}.{column}: колонки нет в БД (нужен шаг в app/migrations.py)")
            elif real[column] != nullable:
                problems.append(
                    f"{table}.{column}: nullable={real[column]} в БД, {nullable} в модели"
                )
    assert not problems, "расхождение схемы с моделями:\n" + "\n".join(problems)


async def test_migrations_are_idempotent(tmp_path):
    """Повторный прогон шагов ничего не меняет: их вызывает КАЖДЫЙ старт контейнера."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'twice.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            first = await run_migrations(conn)
            second = await run_migrations(conn)
    finally:
        await engine.dispose()
    assert second == [], f"повторный прогон изменил схему: {second}"
    assert isinstance(first, list)


async def test_migration_heals_old_database(tmp_path):
    """База, созданная ДО обновления: колонка добавляется, строки получают значение по умолчанию.

    Сценарий реальный: на NAS том с данными живёт годами, а `create_all` новые колонки
    к существующим таблицам не добавляет — только шаги migrations.py.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrations import _m021_help_visibility

    db_path = tmp_path / "old.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        # help_articles в том виде, в каком её создал шаг m020 до появления видимости
        await conn.execute(text(
            """CREATE TABLE help_articles (
                id VARCHAR(36) PRIMARY KEY, kind VARCHAR(10) NOT NULL, slug VARCHAR(80),
                title VARCHAR(300) NOT NULL, category VARCHAR(120) NOT NULL, body TEXT,
                file_path VARCHAR(1024), file_name VARCHAR(512), file_mime VARCHAR(128),
                file_size INTEGER, mandatory_read BOOLEAN NOT NULL,
                created_by_id VARCHAR(36), updated_by_id VARCHAR(36),
                created_at TIMESTAMP, updated_at TIMESTAMP)"""
        ))
        await conn.execute(text(
            "INSERT INTO help_articles (id, kind, title, category, mandatory_read) "
            "VALUES ('a1', 'UPLOAD', 'Старая инструкция', 'Общее', 1)"
        ))

        assert await _m021_help_visibility(conn) is True       # колонка добавлена
        assert await _m021_help_visibility(conn) is False      # идемпотентно

        rows = (await conn.execute(text("SELECT title, visibility FROM help_articles"))).fetchall()
        assert rows == [("Старая инструкция", "ALL")]           # старые статьи остались общедоступными
        indexes = {r[0] for r in (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='help_articles'"))).fetchall()}
        assert "ix_help_articles_visibility" in indexes
    await engine.dispose()


def test_alembic_revision_heals_database_at_previous_head(tmp_path, monkeypatch):
    """База, уже стоящая на ПРЕДЫДУЩЕМ head: колонку добавляет alembic-ревизия.

    Правило docs/DEV-GUIDE.md §4.6 — изменение схемы вносится в обе ветки
    (ревизия `backend/alembic/versions/` И шаг `app/migrations.py`). Проверяем
    alembic-ветку: ею пользуется DBA при `AUTO_MIGRATE=false`, и именно она
    отвечает за боевые базы под alembic. Шаг m021 покрывает ту же ситуацию для
    баз, которые alembic не трогал (см. test_migration_heals_old_database).
    """
    db_path = tmp_path / "at-0013.db"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", sync_url)

    import sqlite3

    from alembic.config import Config

    from alembic import command

    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE help_articles (
            id VARCHAR(36) PRIMARY KEY, kind VARCHAR(10) NOT NULL, slug VARCHAR(80),
            title VARCHAR(300) NOT NULL, category VARCHAR(120) NOT NULL, body TEXT,
            file_path VARCHAR(1024), file_name VARCHAR(512), file_mime VARCHAR(128),
            file_size INTEGER, mandatory_read BOOLEAN NOT NULL,
            created_by_id VARCHAR(36), updated_by_id VARCHAR(36),
            created_at TIMESTAMP, updated_at TIMESTAMP);
        INSERT INTO help_articles (id, kind, title, category, mandatory_read)
            VALUES ('a1', 'UPLOAD', 'Старая инструкция', 'Общее', 1);
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL,
            CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num));
        INSERT INTO alembic_version (version_num) VALUES ('0013_totp_pep');
        CREATE TABLE tasks (
            id VARCHAR(36) PRIMARY KEY, title VARCHAR(300) NOT NULL,
            recurrence VARCHAR(10) NOT NULL DEFAULT 'NONE');
        INSERT INTO tasks (id, title) VALUES ('t1', 'Старая задача');
        CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(320) NOT NULL);
        INSERT INTO users (id, email) VALUES ('u1', 'old@school.test');
        CREATE TABLE substitutions (
            id VARCHAR(36) PRIMARY KEY, absent_user_id VARCHAR(36) NOT NULL,
            substitute_user_id VARCHAR(36) NOT NULL, status VARCHAR(20) NOT NULL,
            note TEXT, confirmed_at TIMESTAMP, created_at TIMESTAMP);
        INSERT INTO substitutions (id, absent_user_id, substitute_user_id, status, note, created_at)
            VALUES ('s1', 'u1', 'u2', 'CONFIRMED', 'передано задач: 2', '2026-01-10 08:00:00');
        INSERT INTO substitutions (id, absent_user_id, substitute_user_id, status, note, created_at)
            VALUES ('s2', 'u2', 'u1', 'ACTIVE', 'без передачи объектов', '2026-01-11 08:00:00');
        -- инвентарь: на него ссылается helpdesk_tickets.equipment_id (ревизия 0019
        -- отказывается создавать таблицы, если FK не на что опереть)
        CREATE TABLE equipment_items (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL);
        -- вложения: ревизия 0020 добавляет к ним колонки автоматической PDF-копии
        CREATE TABLE task_files (
            id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
            storage_path VARCHAR(1024) NOT NULL, original_name VARCHAR(512) NOT NULL,
            mime_type VARCHAR(128) NOT NULL, size_bytes INTEGER NOT NULL, checksum VARCHAR(64),
            uploaded_by_id VARCHAR(36), created_at TIMESTAMP);
        INSERT INTO task_files (id, task_id, storage_path, original_name, mime_type, size_bytes)
            VALUES ('f1', 't1', '2026/01/aa.docx', 'Старый файл.docx',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 1024);
        CREATE TABLE service_note_files (
            id VARCHAR(36) PRIMARY KEY, note_id VARCHAR(36) NOT NULL,
            storage_path VARCHAR(1024) NOT NULL, original_name VARCHAR(512) NOT NULL,
            mime_type VARCHAR(128) NOT NULL, size_bytes INTEGER NOT NULL, checksum VARCHAR(64),
            uploaded_by_id VARCHAR(36), created_at TIMESTAMP);
        """
    )
    con.commit()
    con.close()

    backend_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(cfg, "head")

    schema = _reflect(sync_url)
    assert "visibility" in schema["help_articles"], "ревизия 0014 не добавила help_articles.visibility"
    assert schema["help_articles"]["visibility"] is False, "visibility должна быть NOT NULL"

    # ревизия 0015: история повторов — таблица + сводка на tasks
    assert "task_occurrences" in schema, "ревизия 0015 не создала task_occurrences"
    for col in ("occurrence_cursor", "missed_streak", "last_occurrence_start", "last_occurrence_status"):
        assert col in schema["tasks"], f"ревизия 0015 не добавила tasks.{col}"
    assert schema["tasks"]["missed_streak"] is False, "missed_streak должна быть NOT NULL"
    assert schema["tasks"]["occurrence_cursor"] is True, "occurrence_cursor должна быть nullable"

    # ревизия 0016: персональный грант «все служебные записки» на users
    assert "notes_full_access" in schema["users"], "ревизия 0016 не добавила users.notes_full_access"
    assert schema["users"]["notes_full_access"] is False, "notes_full_access должна быть NOT NULL"

    # ревизия 0017: момент фактической передачи объектов замещающему
    assert "transferred_at" in schema["substitutions"], "ревизия 0017 не добавила substitutions.transferred_at"
    assert schema["substitutions"]["transferred_at"] is True, "transferred_at должна быть nullable"

    # ревизия 0018: грант Владельца «кадровик» (журнал замещений + удаление строк)
    assert "substitutions_access" in schema["users"], "ревизия 0018 не добавила users.substitutions_access"
    assert schema["users"]["substitutions_access"] is False, "substitutions_access должна быть NOT NULL"

    con = sqlite3.connect(db_path)
    assert con.execute("SELECT title, visibility FROM help_articles").fetchall() == [
        ("Старая инструкция", "ALL")
    ], "существующие статьи должны остаться общедоступными"
    assert con.execute("SELECT missed_streak, occurrence_cursor FROM tasks").fetchall() == [(0, None)], (
        "старая задача: провалов нет, курсор истории не инициализирован (задним числом не проваливаем)"
    )

    # старое замещение со сводкой «передано задач» считается переданным — иначе при
    # отмене/завершении объекты не вернулись бы отсутствующему
    assert con.execute("SELECT id, transferred_at FROM substitutions ORDER BY id").fetchall() == [
        ("s1", "2026-01-10 08:00:00"), ("s2", None),
    ], "заполнение transferred_at по сводке прежних версий не сработало"
    indexes = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='help_articles'"
        ).fetchall()
    }
    # ревизия 0019: Helpdesk — три таблицы + грант «системный администратор»
    for table in ("helpdesk_tickets", "helpdesk_messages", "helpdesk_files"):
        assert table in schema, f"ревизия 0019 не создала {table}"
    assert "helpdesk_access" in schema["users"], "ревизия 0019 не добавила users.helpdesk_access"
    assert schema["users"]["helpdesk_access"] is False, "helpdesk_access должна быть NOT NULL"

    # ревизия 0020: PDF-копия документа + несколько вложений у инструкции
    for table in ("task_files", "service_note_files"):
        for col in ("pdf_path", "pdf_size", "pdf_status", "pdf_note"):
            assert col in schema[table], f"ревизия 0020 не добавила {table}.{col}"
        assert schema[table]["pdf_status"] is False, f"{table}.pdf_status должна быть NOT NULL"
    assert "help_article_files" in schema, "ревизия 0020 не создала help_article_files"

    version = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "0020_document_pdf"

    # существующие строки получают значение по умолчанию, а не NULL
    assert con.execute("SELECT pdf_status FROM task_files WHERE id='f1'").fetchone() == ("PENDING",), (
        "у загруженного до обновления файла pdf_status должен быть PENDING (копия доделается при просмотре)"
    )
    assert con.execute("SELECT notes_full_access, substitutions_access, helpdesk_access FROM users").fetchall() == [
        (0, 0, 0)
    ], "существующие сотрудники остаются без грантов — сисадмина назначает Владелец"
    con.close()
    assert "ix_help_articles_visibility" in indexes


def test_every_migration_step_is_registered():
    """Шаг, не попавший в MIGRATIONS, молча не выполнится — проверяем наличие всех _mNNN."""
    import app.migrations as m

    declared = {name for name in dir(m) if name.startswith("_m") and name[2:3].isdigit()}
    registered = {fn.__name__ for fn in MIGRATIONS.values()}
    assert declared - registered == set(), (
        f"шаги объявлены, но не зарегистрированы в MIGRATIONS: {sorted(declared - registered)}"
    )


def test_alembic_path_converges_to_models(tmp_path, monkeypatch):
    """Основной продуктивный путь: `alembic upgrade head` + идемпотентные шаги = модели.

    `app.database.init_db()` делает ровно это в обеих ветках (чистая база и база
    под alembic), поэтому сверяем итоговую схему с метаданными моделей. Цепочка
    alembic отстаёт от моделей (§5.1: в ней нет таблиц модулей 2–4) — их создают
    шаги run_migrations, и это осознанное разделение труда.

    Заодно проверяется, что alembic не падает на ASCII-локали (§5.2).
    """
    from alembic.config import Config

    from alembic import command

    backend_dir = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "alembic.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    # намеренно «враждебная» локаль: alembic.ini читается configparser'ом в кодировке
    # локали, поэтому ASCII-локаль + кириллица в файле = UnicodeDecodeError (§5.2)
    monkeypatch.setenv("LC_ALL", "C")

    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(cfg, "head")

    # страховка init_db(): после alembic всегда прогоняются идемпотентные шаги
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _heal() -> None:
        async with async_engine.begin() as conn:
            await run_migrations(conn)

    asyncio.run(_heal())

    schema = _reflect(f"sqlite:///{db_path}")
    expected = _model_schema()
    assert "alembic_version" in schema

    missing_tables = set(expected) - set(schema)
    assert not missing_tables, f"после alembic + run_migrations нет таблиц: {sorted(missing_tables)}"

    missing_columns = [
        f"{table}.{column}"
        for table, columns in expected.items()
        for column in columns
        if column not in schema.get(table, {})
    ]
    assert not missing_columns, (
        "колонки не создаются ни alembic, ни app/migrations.py "
        f"(нужен новый шаг _mNNN): {missing_columns}"
    )
