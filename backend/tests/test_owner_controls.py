"""Владелец: очистка журнала событий; смена логина учётки — только Владельцем."""
from __future__ import annotations

import pytest

from tests.conftest import auth, new_client

pytestmark = pytest.mark.asyncio


async def test_audit_clear_owner_only(client, seeded):
    from tests.test_totp_pep import pep, sign

    admin = await auth(client, "admin@example.com")
    owner, sig = await sign(client, "owner@example.com")

    # журнал не пуст (логины из auth() уже записаны)
    listing = await admin.get("/audit")
    assert listing.status_code == 200
    assert listing.json()["total"] > 0

    # администрация очистить не может
    resp = await admin.delete("/audit")
    assert resp.status_code == 403

    # владелец может — но только с ПЭП «код + слово»
    unsigned = await owner.request("DELETE", "/audit")
    assert unsigned.status_code == 409 and unsigned.json()["code"] == "TOTP_NOT_BOUND"
    resp = await owner.request("DELETE", "/audit", json=pep(sig))
    assert resp.status_code == 204, resp.text

    listing = await admin.get("/audit")
    body = listing.json()
    assert body["total"] == 1                       # осталась только запись о самой очистке
    assert body["items"][0]["action"] == "AUDIT_CLEAR"
    assert body["items"][0]["changes"]["deleted_records"] > 0


async def test_login_change_owner_only(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")

    resp = await owner.post(
        "/users", json={"full_name": "Логинова Логина", "password": "Passw0rd!", "role": "TEACHER"}
    )
    assert resp.status_code == 201
    user = resp.json()
    assert user["login"] == "loginova.logina"

    # смена логина — только Владелец
    resp = await admin.patch(f"/users/{user['id']}", json={"login": "hacked"})
    assert resp.status_code == 403

    resp = await owner.patch(f"/users/{user['id']}", json={"login": "logina.l"})
    assert resp.status_code == 200
    assert resp.json()["login"] == "logina.l"

    # занятый логин отклоняется
    resp = await owner.patch(f"/users/{user['id']}", json={"login": "logina.l"})
    assert resp.status_code == 200                  # смена на тот же — не ошибка
    resp2 = await owner.post(
        "/users", json={"full_name": "Второй Человек", "password": "Passw0rd!", "role": "TEACHER"}
    )
    second = resp2.json()
    resp = await owner.patch(f"/users/{second['id']}", json={"login": "logina.l"})
    assert resp.status_code == 409

    # вход по новому логину работает
    login = await new_client(client).post(
        "/auth/login", json={"email": "logina.l", "password": "Passw0rd!"}
    )
    assert login.status_code == 200


async def test_create_with_manual_login_owner_only(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")

    resp = await admin.post(
        "/users",
        json={"full_name": "Рукотворный Логин", "password": "Passw0rd!", "role": "TEACHER", "login": "manual.login"},
    )
    assert resp.status_code == 403

    resp = await owner.post(
        "/users",
        json={"full_name": "Рукотворный Логин", "password": "Passw0rd!", "role": "TEACHER", "login": "manual.login"},
    )
    assert resp.status_code == 201
    assert resp.json()["login"] == "manual.login"
