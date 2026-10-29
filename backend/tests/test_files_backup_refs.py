"""Страховка перечня ссылок на файлы хранилища (`files_backup.collect_references`).

Зачем отдельный тест: ZIP-архив «Файлы школы» и сводка строятся от перечня
`collect_references()`. Если новый модуль добавил колонку с путём в хранилище,
а в перечень её не внесли, файл молча уедет в архив под служебным именем
`<uuid>.pdf` и будет числиться «сиротой» — проверка на месте ничего не ломает,
поэтому ловим здесь: каждая модель с «файловой» колонкой обязана упоминаться
в `collect_references()` парой `Model.column`.
"""
from __future__ import annotations

import inspect

from app.services.files_backup import collect_references

#: Имена колонок, в которых модели хранят путь внутри файлового хранилища.
PATH_COLUMNS = {
    "storage_path",   # order_files, task_files, service_note_files, help_article_files, helpdesk_files
    "pdf_path",       # v1.12: автоматическая PDF-копия документа (task/note/help/helpdesk)
    "file_path",      # help_articles (вложение инструкции — наследие до v1.12)
    "file",           # certifications (скан документа)
    "result_file",    # vshk_items (итоговый протокол)
    "avatar",         # users
}


def _models_with_storage_paths() -> list[tuple[str, str]]:
    import app.models  # noqa: F401  — регистрация всех моделей в реестре
    from app.database import Base

    found: list[tuple[str, str]] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for column in sorted({c.key for c in mapper.column_attrs} & PATH_COLUMNS):
            found.append((cls.__name__, column))
    return found


def test_every_storage_path_column_is_collected():
    source = inspect.getsource(collect_references)
    models = _models_with_storage_paths()
    assert models, "не найдено ни одной «файловой» колонки — тест перестал что-либо проверять"

    missing = [f"{name}.{col}" for name, col in models if f"{name}.{col}" not in source]
    assert not missing, (
        "collect_references() не опрашивает: " + ", ".join(missing)
        + " — файлы этих таблиц не попадут в архив «Файлы школы» и будут числиться сиротами"
    )
