"""Локальные учётки без e-mail: генерация логина, массовая загрузка, аватары, смена пароля."""
from __future__ import annotations

import base64

import pytest

from tests.conftest import DEMO_PASSWORD, auth, new_client, upload

pytestmark = pytest.mark.asyncio

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


async def _create_no_email(client, owner, full_name: str, password: str = "Passw0rd!"):
    resp = await owner.post(
        "/users",
        json={"full_name": full_name, "password": password, "role": "TEACHER"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_without_email_generates_login(client, seeded):
    owner = await auth(client, "owner@example.com")
    user = await _create_no_email(client, owner, "Иванова Иванна Петровна")
    assert user["email"] is None
    assert user["login"] == "ivanova.ivanna"

    # вход по логину работает
    resp = await new_client(client).post(
        "/auth/login", json={"email": "ivanova.ivanna", "password": "Passw0rd!"}
    )
    assert resp.status_code == 200, resp.text


async def test_login_collision_gets_suffix(client, seeded):
    owner = await auth(client, "owner@example.com")
    first = await _create_no_email(client, owner, "Иванова Иванна")
    second = await _create_no_email(client, owner, "Иванова Иванна")
    assert first["login"] == "ivanova.ivanna"
    assert second["login"] == "ivanova.ivanna2"


async def test_create_with_email_still_works_and_login_not_needed(client, seeded):
    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/users",
        json={"email": "new.teacher@example.com", "password": "Passw0rd!", "full_name": "Новый Учитель", "role": "TEACHER"},
    )
    assert resp.status_code == 201
    assert resp.json()["login"] is None
    login_resp = await new_client(client).post(
        "/auth/login", json={"email": "new.teacher@example.com", "password": "Passw0rd!"}
    )
    assert login_resp.status_code == 200


async def test_bulk_create_table_fio_password(client, seeded):
    owner = await auth(client, "owner@example.com")
    rows = [
        {"full_name": "Булкина Агния Сергеевна", "password": "Passw0rd!"},
        {"full_name": "Гимназистов Пётр", "password": "Passw0rd!", "email": "gimnazistov@example.com"},
        {"full_name": "Короткий Пароль", "password": "short"},                       # ошибка: пароль < 8
        {"full_name": "Булкина Агния Сергеевна", "password": "Passw0rd!"},          # дубль ФИО — логин с суффиксом
    ]
    resp = await owner.post("/users/bulk", json={"rows": rows})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 4
    assert len(body["created"]) == 3
    assert len(body["failed"]) == 1
    assert body["failed"][0]["row"] == 3
    logins = [c["login"] for c in body["created"]]
    assert logins[0] == "bulkina.agniya"
    assert logins[2] == "bulkina.agniya2"
    assert body["created"][1]["login"] is None          # с e-mail логин не генерируется
    assert body["created"][1]["email"] == "gimnazistov@example.com"

    # вход по сгенерированному логину
    login_resp = await new_client(client).post(
        "/auth/login", json={"email": "bulkina.agniya", "password": "Passw0rd!"}
    )
    assert login_resp.status_code == 200


async def test_bulk_admin_can_only_teachers(client, seeded):
    admin = await auth(client, "admin@example.com")
    resp = await admin.post(
        "/users/bulk",
        json={"rows": [{"full_name": "Хитрый Админ", "password": "Passw0rd!", "role": "ADMIN"}]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] == []
    assert len(body["failed"]) == 1


async def test_avatar_upload_get_delete(client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.post("/users/me/avatar", files=upload("me.png", PNG_1x1, "image/png"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["avatar"]

    # другой пользователь может посмотреть фото
    other = await auth(client, "teacher2@example.com")
    got = await other.get(f"/users/{resp.json()['id']}/avatar")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")

    del_resp = await teacher.delete("/users/me/avatar")
    assert del_resp.status_code == 204
    got2 = await other.get(f"/users/{resp.json()['id']}/avatar")
    assert got2.status_code == 404


async def test_avatar_rejects_non_image(client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.post("/users/me/avatar", files=upload("evil.pdf", b"%PDF-1.4\nstub\n", "application/pdf"))
    assert resp.status_code == 400


async def test_change_password_flow(client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    # неверный текущий пароль
    bad = await teacher.post(
        "/auth/change-password", json={"current_password": "nope", "new_password": "NewPassw0rd!"}
    )
    assert bad.status_code == 401

    ok = await teacher.post(
        "/auth/change-password", json={"current_password": DEMO_PASSWORD, "new_password": "NewPassw0rd!"}
    )
    assert ok.status_code == 204

    # старая сессия отозвана, вход со старым паролем невозможен, с новым — да
    old_login = await new_client(client).post(
        "/auth/login", json={"email": "teacher1@example.com", "password": DEMO_PASSWORD}
    )
    assert old_login.status_code == 401
    new_login = await new_client(client).post(
        "/auth/login", json={"email": "teacher1@example.com", "password": "NewPassw0rd!"}
    )
    assert new_login.status_code == 200
