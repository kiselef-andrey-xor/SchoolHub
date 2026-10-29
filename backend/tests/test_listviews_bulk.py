"""Закрепляемые виды списков («Запомнить этот вид») и массовый редактор сотрудников."""
from __future__ import annotations

import pytest

from tests.conftest import auth

pytestmark = pytest.mark.asyncio


async def test_list_views_pin_and_isolation(client, seeded):
    owner = await auth(client, "owner@example.com")
    teacher = await auth(client, "teacher1@example.com")

    resp = await owner.put("/users/me/list-views/orders", json={"params": {"status": "PUBLISHED", "order_type": "HR"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["params"]["status"] == "PUBLISHED"

    resp = await owner.get("/users/me/list-views")
    assert resp.json()["orders"] == {"status": "PUBLISHED", "order_type": "HR"}

    # у каждого пользователя свой вид
    resp = await teacher.get("/users/me/list-views")
    assert resp.json() == {}
    resp = await teacher.put("/users/me/list-views/tasks", json={"params": {"scope": "mine"}})
    assert resp.status_code == 200
    resp = await owner.get("/users/me/list-views")
    assert "tasks" not in resp.json()

    # раскладка дашборда — тот же механизм
    resp = await owner.put("/users/me/list-views/dashboard", json={"params": {"order": "kpi,plan,main"}})
    assert resp.status_code == 200

    # неизвестный список отклоняется
    resp = await owner.put("/users/me/list-views/hackers", json={"params": {}})
    assert resp.status_code == 400

    resp = await owner.delete("/users/me/list-views/orders")
    assert resp.status_code == 204
    assert "orders" not in (await owner.get("/users/me/list-views")).json()


async def test_bulk_update_positions_and_grants(client, seeded):


    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    t2 = seeded["teacher2@example.com"]

    # справочник должностей сидирован conftest
    resp = await owner.get("/meta/positions")
    positions = resp.json()
    shmo = next((p for p in positions if "ШМО" in p["title"]), positions[0])

    # Владелец: должности + гранты разом
    resp = await owner.post(
        "/users/bulk-update",
        json={
            "user_ids": [t1, t2],
            "position_ids_add": [shmo["id"]],
            "equipment_access": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2

    resp = await owner.get(f"/users/{t1}")
    u = resp.json()
    assert shmo["id"] in [p["id"] for p in u["positions"]]
    assert u["equipment_access"] is True

    # Администрация: гранты трогать нельзя
    resp = await admin.post(
        "/users/bulk-update", json={"user_ids": [t1], "movement_access": True}
    )
    assert resp.status_code == 403

    # Администрация: должности — можно, но Владельца в списке пропускает
    resp = await admin.post(
        "/users/bulk-update",
        json={"user_ids": [t1, seeded["owner@example.com"]], "position_ids_remove": [shmo["id"]]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1 and len(body["skipped"]) == 1

    resp = await owner.get(f"/users/{t1}")
    assert shmo["id"] not in [p["id"] for p in resp.json()["positions"]]
