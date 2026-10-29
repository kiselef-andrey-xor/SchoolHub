"""Режим «Локдаун»: обезличивание персональных данных для всех ролей, кроме Владельца."""
from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import auth

pytestmark = pytest.mark.asyncio


async def _set_lockdown(owner, enabled: bool, sig=None) -> None:
    body = {"enabled": enabled}
    if sig is not None:
        body.update(sig())
    resp = await owner.post("/system/lockdown", json=body)
    assert resp.status_code == 200, resp.text


async def test_lockdown_masks_everyone_but_owner(client, seeded, db_session):
    from app.models.movement import MovementOrder

    db_session.add(
        MovementOrder(
            order_type="ENROLLMENT",
            student="Иванов Пётр Сергеевич",
            guardian="Иванова Мария Петровна",
            order_date=date(2026, 9, 1),
            order_number="123-ОД",
            signatory="Директоров А. А.",
        )
    )
    await db_session.commit()

    from tests.test_totp_pep import sign
    owner, sig = await sign(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    # до включения — всё видно по правам
    resp = await admin.get("/users")
    assert any(u["full_name"] == "Учитель Первый" for u in resp.json()["items"])

    await _set_lockdown(owner, True, sig)

    # Владелец видит настоящие данные
    resp = await owner.get("/users")
    assert any(u["full_name"] == "Учитель Первый" for u in resp.json()["items"])
    assert (await owner.get("/system/lockdown")).json()["masked_for_you"] is False

    # учитель: свой профиль и список людей обезличены
    resp = await teacher.get("/auth/me")
    me = resp.json()
    assert me["full_name"].startswith("Сотрудник ")
    assert me["phone"] is None and me["email"] is None

    resp = await admin.get("/users")
    names = [u["full_name"] for u in resp.json()["items"]]
    assert all(n.startswith("Сотрудник ") for n in names)
    assert all(u["phone"] is None and u["birth_date"] is None for u in resp.json()["items"])

    # движение: обучающиеся и представители — метки «Ученик N» / «Представитель N»
    resp = await owner.patch(f"/users/{seeded['admin@example.com']}", json={"movement_access": True})
    assert resp.status_code == 200
    admin = await auth(client, "admin@example.com")
    resp = await admin.get("/movement")
    row = resp.json()["items"][0]
    assert row["children"][0]["student"].startswith("Ученик ")
    assert row["guardian"].startswith("Представитель ")
    assert row["signatory"].startswith("Сотрудник ")

    # глобальный поиск не выдаёт настоящие ФИО и детей
    resp = await admin.get("/search", params={"q": "Иванов"})
    hit = resp.json()["movement"][0]
    assert hit["title"].startswith("Ученик ")

    # журнал событий обезличен для не-Владельца
    resp = await admin.get("/audit")
    actors = [a["actor_name"] for a in resp.json()["items"] if a["actor_id"]]
    assert actors and all(a.startswith("Сотрудник ") for a in actors)

    # редактирование персональных данных в локдауне отключено (маска не должна попасть в БД)
    resp = await admin.patch(f"/users/{seeded['teacher1@example.com']}", json={"full_name": "ХХ"})
    assert resp.status_code == 400

    await _set_lockdown(owner, False, sig)
    resp = await admin.get("/users")
    assert any(u["full_name"] == "Учитель Первый" for u in resp.json()["items"])
    resp = await admin.get("/movement")
    assert resp.json()["items"][0]["children"][0]["student"] == "Иванов Пётр Сергеевич"


async def test_lockdown_owner_only(client, seeded):
    admin = await auth(client, "admin@example.com")
    resp = await admin.post("/system/lockdown", json={"enabled": True})
    assert resp.status_code == 403
