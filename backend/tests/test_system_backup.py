"""Резервная копия из интерфейса (GET /system/backup).

Главное — регрессия: на sqlite-стенде эндпоинт обязан отвечать понятной 400,
а НЕ падать в 500 из-за непроимпортированных символов (settings, bad_request).
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, export_ticket

pytestmark = pytest.mark.asyncio


async def test_backup_owner_only(client, seeded):
    admin = await auth(client, "admin@example.com")
    resp = await admin.get("/system/backup")
    assert resp.status_code == 403

    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.get("/system/backup")
    assert resp.status_code == 403

    # билет выдаётся только Владельцу: админ не может подписать выгрузку
    resp = await admin.post("/system/export-ticket",
                            json={"target": "db_backup", "pep_code": "000000", "pep_word": "слово"})
    assert resp.status_code == 403


async def test_backup_requires_signed_ticket(client, seeded):
    """Дамп БД — выгрузка всех персональных данных: без подписи ПЭП не отдаётся."""
    owner = await auth(client, "owner@example.com")

    resp = await owner.get("/system/backup")
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "TICKET_MISSING"

    resp = await owner.get("/system/backup", params={"ticket": "выдуманного-билета-не-существует"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "TICKET_INVALID"


async def test_backup_non_postgres_gets_clear_400(client, seeded):
    """Тестовая БД — sqlite: ждём человекочитаемую 400, а не 500 NameError."""
    owner = await auth(client, "owner@example.com")
    ticket = await export_ticket(owner, "owner@example.com", "db_backup")
    resp = await owner.get("/system/backup", params={"ticket": ticket})
    assert resp.status_code == 400, resp.text
    assert "PostgreSQL" in resp.json()["detail"]
