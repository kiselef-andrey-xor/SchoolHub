"""PDF-копия документа + несколько вложений у инструкции.

Revision ID: 0020_document_pdf
Revises: 0019_helpdesk
Create Date: 2026-10-29

Три изменения одной ревизией — все про один сценарий «документ открывается
кликом в новой вкладке»:

  1. колонки `pdf_path` / `pdf_size` / `pdf_status` / `pdf_note` у
     `service_note_files` и `task_files`. Любой загруженный документ
     автоматически приводится к PDF (DOCX — через LibreOffice, фото и текст —
     через ReportLab), оригинал при этом НЕ удаляется. Старые строки получают
     PENDING: копия доделается при первом открытии документа;
  2. таблица `help_article_files` — у инструкции теперь может быть НЕСКОЛЬКО
     документов (положение + форма заявления к нему). Раньше один файл лежал
     прямо в `help_articles.file_*`;
  3. перенос содержимого старых колонок `help_articles.file_*` в новую таблицу.

Сами колонки `help_articles.file_*` НЕ удаляются: их читают резервные копии и
выгрузки прежних версий, а `DROP COLUMN` на боевой базе необратим. Новый код их
не пишет — источником правды стала `help_article_files`.

Параллельно то же изменение делает идемпотентный шаг `_m027_document_pdf`
в `app/migrations.py` (правило docs/DEV-GUIDE.md §4.6 — схема вносится в ОБЕ
ветки). Типы колонок берутся ИЗ МОДЕЛИ.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_document_pdf"
down_revision: Union[str, None] = "0019_helpdesk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILE_TABLES = ("service_note_files", "task_files")
_PDF_COLUMNS = ("pdf_path", "pdf_size", "pdf_status", "pdf_note")
_HELP_FILES = "help_article_files"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _migrate_help_files(bind) -> int:
    """Переносит единственный файл инструкции в новую таблицу (один раз).

    `NOT EXISTS` защищает от повторного запуска: ревизию могут применить дважды
    (откат и накат, восстановление из копии), а дубль вложения был бы виден
    в интерфейсе как два одинаковых документа.
    """
    if not (_has_table("help_articles") and _has_column("help_articles", "file_path")):
        return 0
    if bind.dialect.name == "sqlite":
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
    result = bind.execute(sa.text(f"""
        INSERT INTO {_HELP_FILES}
            (id, article_id, storage_path, original_name, mime_type, size_bytes,
             caption, pdf_status, created_at)
        SELECT {uuid_sql}, h.id, h.file_path,
               COALESCE(h.file_name, 'instruction'),
               COALESCE(h.file_mime, 'application/octet-stream'),
               COALESCE(h.file_size, 0),
               NULL, 'PENDING', {now_sql}
        FROM help_articles h
        WHERE h.file_path IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM {_HELP_FILES} f WHERE f.article_id = h.id)
    """))
    return int(result.rowcount or 0)


def upgrade() -> None:
    from app.models.help import HelpArticleFile
    from app.models.service_note import ServiceNoteFile
    from app.models.task import TaskFile

    bind = op.get_bind()
    models = {"service_note_files": ServiceNoteFile, "task_files": TaskFile}
    for table in _FILE_TABLES:
        if not _has_table(table):
            continue
        columns = models[table].__table__.c
        for name in _PDF_COLUMNS:
            if _has_column(table, name):
                continue
            column = columns[name]
            # pdf_status — NOT NULL + 'PENDING': у уже загруженных файлов копия
            # ещё не строилась, и статус честно говорит «готовится»
            if not column.nullable:
                op.add_column(table, sa.Column(name, column.type, nullable=False,
                                               server_default=sa.text("'PENDING'")))
            else:
                op.add_column(table, sa.Column(name, column.type, nullable=True))

    if not _has_table("help_articles"):
        return
    if not _has_table(_HELP_FILES):
        HelpArticleFile.__table__.create(bind=bind, checkfirst=True)
    _migrate_help_files(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(_HELP_FILES):
        op.drop_table(_HELP_FILES)
    for table in _FILE_TABLES:
        if not _has_table(table):
            continue
        # SQLite не умеет DROP COLUMN — batch_alter_table пересоздаёт таблицу
        with op.batch_alter_table(table) as batch:
            for name in reversed(_PDF_COLUMNS):
                if _has_column(table, name):
                    batch.drop_column(name)
    # файлы инструкций остаются в help_articles.file_*: перенос был копирующим
    del bind
