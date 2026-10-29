"""Учёт оборудования, правка данных школы, напоминания и сводка просрочки."""
from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

from tests.conftest import auth
from tests.test_orders import create_published

pytestmark = pytest.mark.asyncio


async def test_equipment_grant_and_crud(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    aid = seeded["admin@example.com"]

    assert (await admin.get("/equipment")).status_code == 403

    resp = await owner.patch(f"/users/{aid}", json={"equipment_access": True})
    assert resp.status_code == 200
    admin = await auth(client, "admin@example.com")
    assert (await admin.get("/equipment")).status_code == 200

    resp = await admin.post(
        "/equipment",
        json={
            "name": "Ноутбук Lenovo ThinkPad E14",
            "inventory_number": "ИН-000123",
            "serial_number": "SN L12345678",
            "quantity": 2,
            "location": "Кабинет 405",
            "note": "Ответственный: учитель информатики",
        },
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()

    resp = await admin.patch(f"/equipment/{item['id']}", json={"quantity": 3, "location": "Кабинет 406"})
    assert resp.status_code == 200
    assert resp.json()["quantity"] == 3

    resp = await admin.get("/equipment/export.csv")
    assert resp.status_code == 200
    assert "ИН-000123" in resp.text

    # поиск
    resp = await admin.get("/equipment", params={"search": "ThinkPad"})
    assert resp.json()["total"] == 1

    # удаление — только Владелец
    assert (await admin.delete(f"/equipment/{item['id']}")).status_code == 403
    assert (await owner.delete(f"/equipment/{item['id']}")).status_code == 204


async def test_school_name_owner_only(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")

    resp = await admin.patch("/meta/school", json={"school_name": "Хакнутая школа"})
    assert resp.status_code == 403

    resp = await owner.patch(
        "/meta/school",
        json={"school_name": "ОГАОУ «Лицей ядерных технологий» при НИЯУ МИФИ"},
    )
    assert resp.status_code == 200
    got = await admin.get("/meta/school")
    assert got.json()["school_name"] == "ОГАОУ «Лицей ядерных технологий» при НИЯУ МИФИ"


async def test_ack_reminders_and_overdue(client, seeded, db_session):
    import uuid as uuid_lib

    from sqlalchemy import update

    from app.models.order import Order
    from app.services import reminders

    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    order = await create_published(admin, number="901-к")
    oid = uuid_lib.UUID(order["id"])

    # срок завтра → персональное напоминание учителю
    await db_session.execute(update(Order).where(Order.id == oid).values(ack_deadline=date.today() + timedelta(days=1)))
    await db_session.commit()

    sent = await reminders.remind_ack_deadlines(db_session)
    assert sent >= 1
    await db_session.commit()

    listing = (await teacher.get("/notifications")).json()
    assert any(n["type"] == "ORDER_ACK_DEADLINE" for n in listing["items"])

    # срок вчера → сводка администрации + блок дашборда
    await db_session.execute(update(Order).where(Order.id == oid).values(ack_deadline=date.today() - timedelta(days=1)))
    await db_session.commit()

    sent = await reminders.overdue_digest(db_session)
    assert sent >= 1
    await db_session.commit()

    owner = await auth(client, "owner@example.com")
    listing = (await owner.get("/notifications")).json()
    assert any(n["type"] == "ACK_OVERDUE_DIGEST" for n in listing["items"])

    dash = (await admin.get("/dashboard", params={"scope": "school"})).json()
    assert any(a["number"] == "901-к" for a in dash["overdue_acks"])
    assert dash["overdue_acks"][0]["days_overdue"] >= 1


async def test_equipment_csv_import(client, seeded):
    owner = await auth(client, "owner@example.com")
    csv_bytes = (
        "Наименование;Инв. номер;Заводской №;Количество;Местонахождение;Примечание\n"
        "Ноутбук Lenovo;ИН-1;SN1;2;Каб. 405;\n"
        "Проектор Epson;ИН-2;SN2;1;Актовый зал;\n"
        ";ИН-3;;1;;\n"                      # ошибка: пустое наименование
        "МФУ Ricoh;ИН-4;SN4;abc;Каб. 1;\n"  # ошибка: количество
    ).encode("utf-8-sig")
    resp = await owner.post(
        "/equipment/import",
        files={"file": ("inv.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert len(body["failed"]) == 2

    # повторный импорт: ИН-1 обновляется, а не дублируется
    csv2 = "Наименование;Инв. номер;Заводской №;Количество;Местонахождение;Примечание\n" \
           "Ноутбук Lenovo ThinkPad;ИН-1;SN1;5;Каб. 406;\n"
    resp = await owner.post(
        "/equipment/import",
        files={"file": ("inv2.csv", io.BytesIO(csv2.encode("utf-8-sig")), "text/csv")},
    )
    assert resp.json()["updated"] == 1
    listing = (await owner.get("/equipment", params={"search": "ИН-1"})).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["quantity"] == 5 and item["location"] == "Каб. 406"
    assert item["name"] == "Ноутбук Lenovo ThinkPad"
