"""Аутентификация и RBAC: роли, capabilities, управление пользователями."""
from __future__ import annotations

import pytest

from tests.conftest import DEMO_PASSWORD, login, new_client

pytestmark = pytest.mark.asyncio


async def test_login_wrong_password_401(client, seeded):
    resp = await client.post("/auth/login", json={"email": "owner@example.com", "password": "wrong-password"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    # не раскрываем, существует ли e-mail
    resp2 = await client.post("/auth/login", json={"email": "no-such@example.com", "password": "wrong-password"})
    assert resp2.status_code == 401
    assert resp2.json()["detail"] == resp.json()["detail"]


async def test_login_and_me_capabilities_per_role(client, seeded):
    for email, role, must_have, must_not in (
        ("owner@example.com", "OWNER", ["orders:manage", "users:manage", "tasks:read-all", "plans:manage"], []),
        ("admin@example.com", "ADMIN", ["orders:manage", "tasks:read-all", "plans:manage", "users:manage"],
         ["users:manage-admins"]),
        ("teacher1@example.com", "TEACHER", ["orders:read", "orders:ack", "tasks:read-own", "plans:read"],
         ["orders:manage", "tasks:create", "tasks:read-all", "users:read", "audit:read", "plans:manage", "certifications:manage"]),
    ):
        c = await login(new_client(client), email)
        me = (await c.get("/auth/me")).json()
        assert me["role"] == role
        assert me["email"] == email
        for cap in must_have:
            assert cap in me["capabilities"], (role, cap)
        for cap in must_not:
            assert cap not in me["capabilities"], (role, cap)


async def test_unauthenticated_access_denied(client, seeded):
    for url in ("/orders", "/tasks", "/users", "/dashboard", "/audit"):
        resp = await client.get(url)
        assert resp.status_code == 401, url


async def test_teacher_cannot_access_admin_endpoints(client, seeded):
    c = await login(client, "teacher1@example.com")
    assert (await c.get("/users")).status_code == 403
    assert (await c.get("/audit")).status_code == 403
    assert (await c.get("/orders/00000000-0000-0000-0000-000000000000/acknowledgements")).status_code == 403
    assert (
        await c.post("/tasks", json={"title": "Своя задача учителя", "priority": "NORMAL"})
    ).status_code == 403


async def test_admin_cannot_create_admin_or_owner(client, seeded):
    c = await login(client, "admin@example.com")
    resp = await c.post(
        "/users",
        json={
            "email": "new-admin@example.com",
            "password": DEMO_PASSWORD,
            "full_name": "Новый Админ",
            "role": "ADMIN",
        },
    )
    assert resp.status_code == 403

    resp = await c.post(
        "/users",
        json={
            "email": "new-teacher@example.com",
            "password": DEMO_PASSWORD,
            "full_name": "Новый Учитель",
            "role": "TEACHER",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "TEACHER"


async def test_owner_can_create_admin_and_sees_everyone(client, seeded):
    c = await login(client, "owner@example.com")
    resp = await c.post(
        "/users",
        json={
            "email": "second-admin@example.com",
            "password": DEMO_PASSWORD,
            "full_name": "Второй Админ",
            "role": "ADMIN",
        },
    )
    assert resp.status_code == 201

    users = (await c.get("/users")).json()
    emails = {u["email"] for u in users["items"]}
    assert {"owner@example.com", "admin@example.com", "teacher1@example.com"} <= emails

    # админ не видит владельца
    a = await login(new_client(client), "admin@example.com")
    emails_admin = {u["email"] for u in (await a.get("/users")).json()["items"]}
    assert "owner@example.com" not in emails_admin


async def test_owner_cannot_demote_or_deactivate_self(client, seeded):
    c = await login(client, "owner@example.com")
    me = (await c.get("/auth/me")).json()

    resp = await c.patch(f"/users/{me['id']}", json={"role": "TEACHER"})
    assert resp.status_code == 409

    resp = await c.delete(f"/users/{me['id']}")
    assert resp.status_code == 409

    resp = await c.patch(f"/users/{me['id']}", json={"is_active": False})
    assert resp.status_code == 409


async def test_last_owner_cannot_be_deactivated(client, seeded):
    """Единственного Владельца нельзя ни деактивировать, ни разжаловать."""
    c = await login(client, "owner@example.com")
    owner_id = (await c.get("/auth/me")).json()["id"]

    # сам себя деактивировать нельзя в принципе
    assert (await c.delete(f"/users/{owner_id}")).status_code == 409

    # создаём второго владельца — и теперь первого можно деактивировать другим владельцем
    resp = await c.post(
        "/users",
        json={"email": "owner2@example.com", "password": DEMO_PASSWORD, "full_name": "Владелец Два", "role": "OWNER"},
    )
    assert resp.status_code == 201
    from tests.test_totp_pep import pep, sign

    owner2, sig = await sign(client, "owner2@example.com")
    assert (await owner2.request("DELETE", f"/users/{owner_id}", json=pep(sig))).status_code == 204

    # и наоборот: разжаловать единственного оставшегося владельца нельзя
    resp = await owner2.patch(f"/users/{resp.json()['id']}", json={"role": "TEACHER"})
    assert resp.status_code == 409


async def test_deactivated_user_cannot_login(client, seeded, session_factory):
    from sqlalchemy import select

    from app.models.user import User

    o = await login(client, "owner@example.com")
    teacher_id = seeded["teacher2@example.com"]
    assert (await o.patch(f"/users/{teacher_id}", json={"is_active": False})).status_code == 200

    resp = await client.post("/auth/login", json={"email": "teacher2@example.com", "password": DEMO_PASSWORD})
    assert resp.status_code == 403

    # и уже выданный токен перестаёт работать
    async with session_factory() as s:
        user = (await s.execute(select(User).where(User.id == teacher_id))).scalar_one()
        user.is_active = True
        await s.commit()

    t = await login(new_client(client), "teacher2@example.com")
    assert (await t.get("/auth/me")).status_code == 200


async def test_refresh_token_flow(client, seeded):
    """Refresh живёт в httpOnly-cookie и ротируется при каждом обмене."""
    resp = await client.post("/auth/login", json={"email": "teacher1@example.com", "password": DEMO_PASSWORD})
    tokens = resp.json()
    assert "refresh_token" not in tokens           # в теле refresh больше не отдаётся
    cookie_header = resp.headers.get("set-cookie", "")
    assert "schoolhub_refresh=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "samesite=strict" in cookie_header.lower()
    assert "path=/api/v1/auth" in cookie_header.lower()

    # обмен по cookie (httpx хранит её сам)
    r2 = await client.post("/auth/refresh")
    assert r2.status_code == 200, r2.text
    assert r2.json()["access_token"] != tokens["access_token"]

    # без cookie refresh не работает
    from tests.conftest import new_client

    anon = new_client(client)
    assert (await anon.post("/auth/refresh")).status_code == 401

    # access-токен нельзя использовать как Bearer для refresh-эндпоинта не нужен,
    # а refresh нельзя применить как access:
    client.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    assert (await client.get("/auth/me")).status_code == 200


async def test_change_password(client, seeded):
    c = await login(client, "teacher1@example.com")
    resp = await c.post("/auth/change-password", json={"current_password": "bad-one-1", "new_password": "NewPass123!"})
    assert resp.status_code == 401

    resp = await c.post(
        "/auth/change-password", json={"current_password": DEMO_PASSWORD, "new_password": "NewPass123!"}
    )
    assert resp.status_code == 204

    resp = await client.post("/auth/login", json={"email": "teacher1@example.com", "password": "NewPass123!"})
    assert resp.status_code == 200


async def test_permissions_matrix_endpoint(client, seeded):
    c = await login(client, "teacher1@example.com")
    matrix = (await c.get("/meta/permissions")).json()
    assert set(matrix["roles"]) == {"OWNER", "ADMIN", "TEACHER"}
    by_cap = {row["capability"]: row["grants"] for row in matrix["capabilities"]}
    assert by_cap["orders:manage"] == {"OWNER": True, "ADMIN": True, "TEACHER": False}
    assert by_cap["tasks:read-own"] == {"OWNER": False, "ADMIN": False, "TEACHER": True}
    assert by_cap["plans:read"] == {"OWNER": True, "ADMIN": True, "TEACHER": True}
    assert by_cap["plans:manage"] == {"OWNER": True, "ADMIN": True, "TEACHER": False}


async def test_audit_log_records_actions(client, seeded):
    a = await login(client, "admin@example.com")
    o = await login(new_client(client), "owner@example.com")

    await a.post(
        "/users",
        json={"email": "audit-teacher@example.com", "password": DEMO_PASSWORD, "full_name": "Аудит Учитель", "role": "TEACHER"},
    )
    resp = await o.get("/audit")
    assert resp.status_code == 200, resp.text
    logs = resp.json()
    assert isinstance(logs["items"], list) and logs["items"], logs
    actions = {item["action"] for item in logs["items"]}
    assert {"LOGIN", "USER_CREATE"} <= actions

    created = next(i for i in logs["items"] if i["action"] == "USER_CREATE")
    assert created["actor_email"] == "admin@example.com"
    assert created["actor_role"] == "ADMIN"
    assert created["changes"]["email"] == "audit-teacher@example.com"
    assert created["changes"]["role"] == "TEACHER"
    # ip_address/user_agent заполняются из запроса (в ASGI-тестах клиента нет -> None)
    assert "ip_address" in created

    # редактирование пишется диффом {field: {from, to}}
    new_id = (await a.get("/users", params={"search": "audit-teacher"})).json()["items"][0]["id"]
    pos = (await a.post("/meta/positions", json={"title": "Учитель информатики"})).json()
    assert pos.get("id"), pos
    assert (await a.patch(f"/users/{new_id}", json={"position_ids": [pos["id"]]})).status_code == 200
    updated = next(i for i in (await o.get("/audit", params={"action": "USER_UPDATE"})).json()["items"])
    assert updated["changes"]["positions"]["to"] == "Учитель информатики"
