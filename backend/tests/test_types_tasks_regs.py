"""Колонки входящего/регистрации в задачах, ручной № регистрации, динамические типы приказов,
задачи владельцу от администрации."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.conftest import auth
from tests.test_tasks import task_payload

pytestmark = pytest.mark.asyncio


async def test_admin_can_assign_task_to_owner(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner_id = seeded["owner@example.com"]

    # владелец присутствует в справочнике исполнителей у администрации
    options = (await admin.get("/users/options")).json()
    assert any(u["id"] == owner_id for u in options)

    resp = await admin.post("/tasks", json=task_payload([owner_id], title="Проверить отчёт владельцу"))
    assert resp.status_code == 201, resp.text
    tid = resp.json()["id"]

    owner = await auth(client, "owner@example.com")
    mine = (await owner.get("/tasks", params={"scope": "mine"})).json()
    assert any(t["id"] == tid for t in mine["items"])


async def test_incoming_reg_manual_and_tasks_columns(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")

    # задача с входящим номером → запись журнала
    resp = await admin.post("/tasks", json=task_payload([seeded["teacher1@example.com"]], incoming_number="Вх-77"))
    assert resp.status_code == 201
    t1 = resp.json()
    resp = await admin.post("/tasks", json=task_payload([seeded["teacher1@example.com"]], incoming_number="Вх-78"))
    assert resp.status_code == 201
    t2 = resp.json()

    listing = (await owner.get("/incoming")).json()["items"]
    reg_by_number = {r["incoming_number"]: r for r in listing}
    rid_77 = reg_by_number["Вх-77"]["id"]

    # ручной номер регистрации + контроль дублей
    resp = await owner.patch(f"/incoming/{rid_77}", json={"reg_no": 500})
    assert resp.status_code == 200
    assert resp.json()["reg_no"] == 500
    rid_78 = reg_by_number["Вх-78"]["id"]
    resp = await owner.patch(f"/incoming/{rid_78}", json={"reg_no": 500})
    assert resp.status_code == 409

    # колонки в задачах: входящий № и № регистрации
    tasks = (await admin.get("/tasks", params={"sort": "reg"})).json()
    by_id = {t["id"]: t for t in tasks["items"]}
    assert by_id[t1["id"]]["incoming_number"] == "Вх-77"
    assert by_id[t1["id"]]["incoming_reg"] == "500/" + str(datetime.now(timezone.utc).year)
    # сортировка по убыванию регистрации: Вх-77 (500) выше Вх-78 (авто=1..2)
    idx1 = [t["id"] for t in tasks["items"]].index(t1["id"])
    idx2 = [t["id"] for t in tasks["items"]].index(t2["id"])
    assert idx1 < idx2


async def test_order_types_dynamic(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")
    owner = await auth(client, "owner@example.com")

    types = (await teacher.get("/meta/order-types")).json()
    codes = {t["code"] for t in types}
    assert {"HR", "EDUCATION", "ADMIN"} <= codes

    # добавляет администрация
    resp = await admin.post("/meta/order-types", json={"title": "По питанию"})
    assert resp.status_code == 201, resp.text
    code = resp.json()["code"]

    # приказ с новым типом
    from tests.test_orders import order_payload

    resp = await admin.post("/orders", json=order_payload(number="700-к", order_type=code))
    assert resp.status_code == 201, resp.text
    oid = resp.json()["id"]

    # учитель не управляет типами
    assert (await teacher.post("/meta/order-types", json={"title": "Хак"})).status_code == 403

    # занятый тип не удаляется
    resp = await owner.delete(f"/meta/order-types/{code}")
    assert resp.status_code == 409

    # неизвестный тип отклоняется
    resp = await admin.post("/orders", json=order_payload(number="701-к", order_type="NOPE"))
    assert resp.status_code in (400, 422)

    # свободный тип удаляется
    resp = await admin.post("/meta/order-types", json={"title": "Временный"})
    tmp = resp.json()["code"]
    assert (await owner.delete(f"/meta/order-types/{tmp}")).status_code == 204
    assert oid  # приказ остался нетронутым
