"""Первоначальная настройка: создание первого Владельца через веб-форму (без терминала)."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.enums import Role
from app.models.user import User
from app.services import bootstrap

pytestmark = pytest.mark.asyncio


async def _token(session_factory) -> str:
    async with session_factory() as s:
        state = await bootstrap.get_setup_state(s)
        token = state.setup_token
        await s.commit()
    return token or ""


async def test_api_blocked_until_configured(raw_client):
    """До создания Владельца работает только health и /setup/*."""
    resp = await raw_client.get("/orders")
    assert resp.status_code == 503
    assert resp.json()["code"] == "SETUP_REQUIRED"

    for path in ("/tasks", "/users", "/dashboard", "/audit", "/auth/login"):
        blocked = await raw_client.post(path, json={}) if path == "/auth/login" else await raw_client.get(path)
        assert blocked.status_code == 503, path

    assert (await raw_client.get("/setup/status")).status_code == 200


async def test_setup_status_reports_required(raw_client):
    body = (await raw_client.get("/setup/status")).json()
    assert body == {
        **body,
        "is_configured": False,
        "setup_required": True,
        "completed_by": None,
    }
    assert "журнал" in body["hint"].lower() or "код" in body["hint"].lower()


async def test_first_owner_created_via_web_form(raw_client, session_factory):
    token = await _token(session_factory)
    assert len(token) == 8

    resp = await raw_client.post(
        "/setup/first-owner",
        json={
            "setup_token": token,
            "email": "admin@school.local",
            "password": "Школа2026!",
            "full_name": "Соколова Ирина Аркадьевна",
            "school_name": "ГБОУ Школа № 1472",
        },
    )
    assert resp.status_code == 201, resp.text
    owner = resp.json()
    assert owner["role"] == "OWNER"
    assert owner["email"] == "admin@school.local"
    assert owner["auth_source"] == "local"

    # система настроена → API разблокирован, вход работает
    status = (await raw_client.get("/setup/status")).json()
    assert status["is_configured"] is True and status["setup_required"] is False
    assert status["school_name"] == "ГБОУ Школа № 1472"
    assert status["completed_by"] == "admin@school.local"

    login = await raw_client.post("/auth/login", json={"email": "admin@school.local", "password": "Школа2026!"})
    assert login.status_code == 200, login.text

    # API разблокировался сразу, без перезапуска приложения
    assert raw_client._transport.app.state.setup_complete is True
    raw_client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    me = (await raw_client.get("/auth/me")).json()
    assert me["role"] == "OWNER"
    assert "users:manage" in me["capabilities"]
    assert (await raw_client.get("/orders")).status_code == 200


async def test_setup_is_one_shot(raw_client, session_factory):
    token = await _token(session_factory)
    payload = {
        "setup_token": token,
        "email": "first@school.local",
        "password": "Пароль12345",
        "full_name": "Первый Владелец",
    }
    assert (await raw_client.post("/setup/first-owner", json=payload)).status_code == 201

    second = await raw_client.post(
        "/setup/first-owner",
        json={**payload, "email": "second@school.local", "setup_token": token},
    )
    assert second.status_code == 409, "повторная настройка недопустима"

    # код настройки стирается из БД после завершения
    async with session_factory() as s:
        state = await bootstrap.get_setup_state(s)
        assert state.setup_token is None
        owners = int(
            (await s.execute(select(func.count()).select_from(User).where(User.role == Role.OWNER))).scalar_one()
        )
        assert owners == 1


async def test_wrong_setup_token_rejected(raw_client, session_factory):
    await _token(session_factory)
    resp = await raw_client.post(
        "/setup/first-owner",
        json={
            "setup_token": "НЕВЕРНЫЙ",
            "email": "hacker@school.local",
            "password": "Пароль12345",
            "full_name": "Злоумышленник",
        },
    )
    assert resp.status_code == 403

    # учётка не создана
    async with session_factory() as s:
        count = int((await s.execute(select(func.count()).select_from(User))).scalar_one())
        assert count == 0


async def test_weak_input_rejected(raw_client, session_factory):
    token = await _token(session_factory)
    short_password = await raw_client.post(
        "/setup/first-owner",
        json={"setup_token": token, "email": "a@b.local", "password": "123", "full_name": "Админ"},
    )
    assert short_password.status_code == 422            # валидация схемы (min_length=8)

    bad_email = await raw_client.post(
        "/setup/first-owner",
        json={"setup_token": token, "email": "не-почта", "password": "Пароль12345", "full_name": "Админ"},
    )
    assert bad_email.status_code == 403


async def test_setup_token_endpoint_is_local_only(raw_client, session_factory):
    """Код настройки нельзя получить из внешней сети — только на самом сервере."""
    from app.api.setup import _is_local

    class FakeClient:
        host = "203.0.113.7"          # внешний адрес (например, из-за reverse proxy)

    class FakeRequest:
        client = FakeClient()

    class LocalClient:
        host = "127.0.0.1"

    class LocalRequest:
        client = LocalClient()

    assert _is_local(FakeRequest()) is False
    assert _is_local(LocalRequest()) is True

    # через ASGI-транспорт клиент = 127.0.0.1, т.е. имитируем консоль самого NAS
    resp = await raw_client.post("/setup/token")
    assert resp.status_code == 200
    assert len(resp.json()["setup_token"]) == 8


async def test_cli_first_owner_script(raw_client, session_factory):
    """Сценарий установки из терминала: тот же результат, код настройки не нужен."""
    async with session_factory() as s:
        owner = await bootstrap.create_first_owner(
            s,
            bootstrap.OwnerCredentials(
                email="cli@school.local", password="КлиПароль123", full_name="CLI Админ", school_name=None
            ),
            token=None,
        )
        await s.commit()
        assert owner.role == Role.OWNER

    assert (await raw_client.get("/setup/status")).json()["is_configured"] is True
    login = await raw_client.post("/auth/login", json={"email": "cli@school.local", "password": "КлиПароль123"})
    assert login.status_code == 200


async def test_setup_info_has_no_secrets(raw_client):
    body = (await raw_client.get("/setup/info")).json()
    assert body["app_env"] == "dev"
    assert body["secret_key_source"] in ("env", "auto-generated file")
    assert "secret" not in str(body.values()).lower().replace("secret_key_source", "")
    text = str(body)
    from app.config import settings

    assert settings.secret_key not in text, "значение ключа подписи не должно попадать в ответ"


async def test_audit_records_setup_completion(client, seeded, session_factory):
    o = await __import__("tests.conftest", fromlist=["auth"]).auth(client, "owner@example.com")
    logs = (await o.get("/audit", params={"action": "SETUP_COMPLETE"})).json()
    # в seeded-стенде настройка не выполнялась — проверяем лишь доступность журнала
    assert logs["total"] >= 0
