"""Движение обучающихся, подробная история задач, ФИО в журнале событий."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth, new_client, upload
from tests.test_orders import pdf_bytes
from tests.test_tasks import task_payload

pytestmark = pytest.mark.asyncio


async def test_movement_access_and_crud(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    aid = seeded["admin@example.com"]

    # по умолчанию раздел только у Владельца
    assert (await admin.get("/movement")).status_code == 403

    # грант выдаёт Владелец из карточки пользователя
    resp = await owner.patch(f"/users/{aid}", json={"movement_access": True})
    assert resp.status_code == 200
    assert resp.json()["movement_access"] is True
    admin = await auth(client, "admin@example.com")
    assert (await admin.get("/movement")).status_code == 200

    # перевод: нужны «откуда» и «куда»
    resp = await admin.post(
        "/movement",
        json={
            "order_type": "TRANSFER",
            "guardian": "Дёмин С.А.",
            "student": "Дёмина Ярослава Сергеевна",
            "birth_date": "2014-01-26",
            "from_place": "7Г класс",
            "to_place": "7Д класс",
            "order_date": "2026-10-19",
            "order_number": "119-У",
            "signatory": "И.В. Астраханцева",
        },
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "NEW"

    resp = await admin.post(
        "/movement",
        json={
            "order_type": "TRANSFER", "guardian": "X", "student": "Y", "birth_date": "2014-01-01",
            "to_place": "7Д", "order_date": "2026-10-19", "order_number": "120-У",
        },
    )
    assert resp.status_code == 422   # нет «откуда» для перевода

    # статусы
    resp = await admin.patch(f"/movement/{row['id']}", json={"status": "DONE"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "DONE"

    # сводка CSV по выбранным
    resp = await admin.get("/movement/export.csv", params={"ids": row["id"]})
    assert resp.status_code == 200
    body = resp.text
    assert "Дёмина Ярослава Сергеевна" in body and "7Г класс" in body and "выполнен" in body

    # удаление — только Владелец
    assert (await admin.delete(f"/movement/{row['id']}")).status_code == 403
    assert (await owner.delete(f"/movement/{row['id']}")).status_code == 204


async def test_task_history_covers_actions(client, seeded):
    admin = await auth(client, "admin@example.com")
    resp = await admin.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))
    assert resp.status_code == 201
    task = resp.json()
    tid = task["id"]

    resp = await admin.post(
        f"/tasks/{tid}/files", files=upload("prikaz.pdf", pdf_bytes("Приказ"), "application/pdf")
    )
    assert resp.status_code == 201
    resp = await admin.patch(f"/tasks/{tid}", json={"deadline": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()})
    assert resp.status_code == 200

    detail = (await admin.get(f"/tasks/{tid}")).json()
    notes = [h["note"] for h in detail["history"]]
    kinds = [h["to_status"] for h in detail["history"]]
    assert "EVENT" in kinds
    joined = " | ".join(n for n in notes if n)
    assert "Назначены исполнители" in joined
    assert "Добавлен файл" in joined
    assert "Срок" in joined
    # у EVENT-записей есть исполнитель
    event = next(h for h in detail["history"] if h["to_status"] == "EVENT")
    assert event["actor"] is not None


async def test_audit_shows_full_name_without_email(client, seeded):
    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/users", json={"full_name": "Безпочтова Анна Ивановна", "password": "Passw0rd!", "role": "TEACHER"}
    )
    assert resp.status_code == 201
    login = resp.json()["login"]

    user_client = await new_client(client).post("/auth/login", json={"email": login, "password": "Passw0rd!"})
    assert user_client.status_code == 200

    listing = (await owner.get("/audit", params={"action": "LOGIN"})).json()
    entry = next(e for e in listing["items"] if e["actor_name"] == "Безпочтова Анна Ивановна")
    assert entry["actor_email"] is None


async def test_movement_children_and_template(client, seeded):
    owner = await auth(client, "owner@example.com")

    # приказ с двумя детьми и без законного представителя
    resp = await owner.post(
        "/movement",
        json={
            "order_type": "ENROLLMENT",
            "children": [
                {"student": "Кривову Елену Николаевну", "birth_date": "2013-06-09", "to_place": "7Е"},
                {"student": "Иванова Ивана Ивановича", "birth_date": "2013-01-01", "to_place": "7Е"},
            ],
            "order_date": "2026-10-15",
            "order_number": "118-У",
            "signatory": "И.В. Астраханцева",
        },
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert len(row["children"]) == 2
    assert row["guardian"] is None

    # общий шаблон печатной формы
    resp = await owner.get("/movement/template")
    assert resp.status_code == 200
    assert "{BODY}" in resp.json()["default"]
    resp = await owner.put("/movement/template", json={"template": "ПРИКАЗЫВАЮ:\n{BODY}\n{CONTROL}\nПодписант: {SIGNATORY}"})
    assert resp.status_code == 200
    resp = await owner.get("/movement/template")
    assert resp.json()["template"] == "ПРИКАЗЫВАЮ:\n{BODY}\n{CONTROL}\nПодписант: {SIGNATORY}"
    # сброс
    resp = await owner.put("/movement/template", json={"template": None})
    assert resp.json()["template"] is None

    # без детей приказ создать нельзя
    resp = await owner.post(
        "/movement",
        json={"order_type": "TRANSFER", "order_date": "2026-10-15", "order_number": "120-У",
              "from_place": "6А", "to_place": "7А", "children": []},
    )
    assert resp.status_code == 422


async def test_equipment_moves_and_month_report(client, seeded):
    from datetime import date

    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/equipment",
        json={"name": "Проектор Epson", "inventory_number": "ИН-9", "quantity": 1, "location": "Каб. 405"},
    )
    assert resp.status_code == 201
    item = resp.json()

    resp = await owner.post(
        f"/equipment/{item['id']}/move",
        json={"to_location": "Каб. 321", "note": "перенос по заявке завхоза"},
    )
    assert resp.status_code == 200, resp.text
    mv = resp.json()
    assert mv["from_location"] == "Каб. 405" and mv["to_location"] == "Каб. 321"

    # текущая позиция обновилась
    got = (await owner.get("/equipment", params={"search": "ИН-9"})).json()["items"][0]
    assert got["location"] == "Каб. 321"

    month = date.today().isoformat()[:7]
    resp = await owner.get("/equipment/moves", params={"month": month})
    assert resp.status_code == 200
    moves = resp.json()
    assert len(moves) == 1 and moves[0]["item_name"] == "Проектор Epson"

    resp = await owner.get("/equipment/moves/export.csv", params={"month": month})
    assert resp.status_code == 200 and "Каб. 321" in resp.text

    # другой месяц пуст; плохой формат месяца отклоняется
    assert (await owner.get("/equipment/moves", params={"month": "2000-01"})).json() == []
    assert (await owner.get("/equipment/moves", params={"month": "октябрь"})).status_code == 400
