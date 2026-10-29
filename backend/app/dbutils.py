"""Утилиты уровня БД: переносимый регистронезависимый поиск.

Проблема: `ILIKE` в PostgreSQL корректно работает с Unicode («Гамма» ~ «гам»),
а в SQLite и `LIKE`, и встроенная `lower()` — только ASCII. Чтобы один и тот же
код одинаково искал в prod (PostgreSQL) и в демо/тестах (SQLite), реализация
выбирается по диалекту:

  * postgresql -> нативный `column ILIKE '%needle%'`;
  * sqlite     -> `fold_lower(column) LIKE '%needle%'`, где `fold_lower` — SQL-функция,
                  зарегистрированная на соединении и использующая `str.lower()` из Python
                  (регистрация: `register_sqlite_functions(engine)`);
  * если функция недоступна — деградируем до ASCII-`lower()` (без падения запроса).
"""
from __future__ import annotations

import logging

from sqlalchemy import String, event, func
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger("schoolhub.db")

FOLD_FN = "fold_lower"
_fold_registered = False


def register_sqlite_functions(engine: AsyncEngine) -> bool:
    """Регистрирует Unicode-aware `fold_lower` на каждом новом DBAPI-соединении SQLite."""
    global _fold_registered
    if engine.dialect.name != "sqlite":
        return _fold_registered

    ok = True

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_connection, _record) -> None:
        try:
            dbapi_connection.create_function(FOLD_FN, 1, lambda v: v.lower() if isinstance(v, str) else v)
        except Exception as exc:  # pragma: no cover - зависит от драйвера
            logger.warning("Не удалось зарегистрировать %s: %s", FOLD_FN, exc)

    _fold_registered = ok
    return ok


def fold_available() -> bool:
    return _fold_registered


def escape_like(needle: str) -> str:
    r"""Экранирует спецсимволы LIKE (`%`, `_`, сам экранирующий `\`).

    Без этого пользовательский ввод читается как шаблон: поиск по «%» открывает
    всю таблицу (лишний полный скан на боевой базе), «_» заменяет любой символ,
    а «100%» не находится вовсе. Значения из запроса — это текст, а не шаблон.
    """
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def ilike(column, needle: str) -> ColumnElement:
    """Регистронезависимое «содержит» для строковой колонки (переносимо между диалектами)."""
    from app.database import engine  # локальный импорт: избегаем цикла инициализации

    needle = escape_like(needle.strip())
    pattern = f"%{needle}%"
    dialect = engine.dialect.name

    if dialect == "postgresql":
        return column.ilike(pattern, escape="\\")           # Unicode-aware из коробки
    if dialect == "sqlite" and fold_available():
        return getattr(func, FOLD_FN)(func.cast(column, String)).like(pattern.lower(), escape="\\")
    # ASCII-фолбэк
    return func.lower(func.cast(column, String)).like(pattern.lower(), escape="\\")
