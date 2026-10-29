"""Проверка значения штрихкода: никакой разметки и управляющих символов.

Зачем: штрихкод попадает в SVG этикетки, а фронтенд вставляет его через
dangerouslySetInnerHTML — значение с «<»/«"» стало бы хранимым XSS на карточке
позиции (причём завести его можно и вручную, и импортом CSV). Фронтенд значения
экранирует (src/lib/barcode.ts), а здесь проверяем вторую линию защиты — API.

Список запрещённых символов (а не разрешённых) — намеренно: в действующих школах
в поле штрихкода могли записать кириллицу или «№», и белый список заблокировал бы
правка любой такой карточки после обновления.
"""
from __future__ import annotations

import csv
import io

import pytest

from tests.conftest import auth, upload

pytestmark = pytest.mark.asyncio

XSS_BARCODE = '"><script>alert(1)</script>'


async def test_barcode_rejects_markup_on_create_and_update(client, seeded):
    owner = await auth(client, "owner@example.com")

    resp = await owner.post("/equipment", json={"name": "ПЭВМ", "barcode": XSS_BARCODE})
    assert resp.status_code == 422, resp.text
    assert "Штрихкод не должен содержать" in resp.text

    resp = await owner.post("/equipment", json={"name": "ПЭВМ", "inventory_number": "ИН-1", "quantity": 1})
    assert resp.status_code == 201, resp.text
    item = resp.json()

    resp = await owner.patch(f"/equipment/{item['id']}", json={"barcode": XSS_BARCODE})
    assert resp.status_code == 422, resp.text
    # разметка и управляющие символы отклоняются, обычный текст — нет
    for bad in ["a&b", "code<script>", "многострочный\nкод", "с\tтабуляцией"]:
        resp = await owner.patch(f"/equipment/{item['id']}", json={"barcode": bad})
        assert resp.status_code == 422, (bad, resp.text)
    # пробелы по краям снимаются, а не отклоняются
    for good in ["INV-12/3 A", "ИНВ-123", "№5-12", "4600000000017", "  4600000000024  "]:
        resp = await owner.patch(f"/equipment/{item['id']}", json={"barcode": good})
        assert resp.status_code == 200, (good, resp.text)
        assert resp.json()["barcode"] == good.strip()
    # пустая строка снимает штрихкод, а не сохраняет «»
    resp = await owner.patch(f"/equipment/{item['id']}", json={"barcode": "   "})
    assert resp.status_code == 200 and resp.json()["barcode"] is None


async def test_barcode_import_reports_bad_row(client, seeded):
    owner = await auth(client, "owner@example.com")
    # файл собираем csv-писателем: кавычки в «остром» значении должны быть
    # экранированы по правилам формата, как это сделал бы Excel
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Наименование", "Инвентарный номер", "Заводской (серийный) номер",
                "Штрихкод", "Количество", "Местонахождение", "Примечание"])
    w.writerow(["Проектор", "ИН-10", "SN-10", XSS_BARCODE, "1", "Кабинет 12", ""])
    w.writerow(["Ноутбук", "ИН-11", "SN-11", "4600000000017", "2", "Кабинет 13", ""])
    resp = await owner.post(
        "/equipment/import", files=upload("inv.csv", buf.getvalue().encode("utf-8"), "text/csv")
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["created"] == 1, result
    assert len(result["failed"]) == 1, result
    assert result["failed"][0]["row"] == 1
    assert "Штрихкод не должен содержать" in result["failed"][0]["reason"]

    # опасное значение не осело ни в одной позиции
    listing = await owner.get("/equipment", params={"search": "ИН-10"})
    assert listing.json()["items"] == [], listing.text


async def test_barcode_from_export_round_trips(client, seeded):
    """Свой же экспорт должен импортироваться без замечаний."""
    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/equipment",
        json={"name": "МФУ Canon", "inventory_number": "ИН-20", "barcode": "4600000000024", "quantity": 1},
    )
    assert resp.status_code == 201, resp.text
    exported = await owner.get("/equipment/export.csv")
    assert exported.status_code == 200
    result = (
        await owner.post("/equipment/import", files=upload("exp.csv", exported.content, "text/csv"))
    ).json()
    assert result["failed"] == [], result
    assert result["updated"] >= 1, result
