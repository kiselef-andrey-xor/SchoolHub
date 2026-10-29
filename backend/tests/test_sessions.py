"""Сессии (refresh-токены): ротация, отзыв, детект повторного использования, блокировка."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import DEMO_PASSWORD, auth, new_client

pytestmark = pytest.mark.asyncio


async def _login(client: AsyncClient, email: str, remember: bool = True) -> tuple[str, str]:
    """Вход отдельным клиентом: возвращает (access, refresh-cookie)."""
    resp = await client.post(
        "/auth/login", json={"email": email, "password": DEMO_PASSWORD, "remember": remember}
    )
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get("schoolhub_refresh")
    assert cookie, "refresh-cookie не установлена"
    return resp.json()["access_token"], cookie


# --------------------------------------------------------------------- ротация
async def test_refresh_rotates_and_old_token_dies_after_grace(client, seeded, monkeypatch):
    from app.config import settings

    c = new_client(client)
    _, refresh1 = await _login(c, "teacher1@example.com")

    # первый обмен — успешен, cookie ротируется
    r = await c.post("/auth/refresh")
    assert r.status_code == 200
    refresh2 = r.cookies.get("schoolhub_refresh")
    assert refresh2 and refresh2 != refresh1

    # предъявляем СТАРЫЙ токен вне льготного окна — это считается кражей
    monkeypatch.setattr(settings, "refresh_reuse_grace_sec", 0)
    stale = new_client(client)
    stale.cookies.set("schoolhub_refresh", refresh1, path="/api/v1/auth")
    r2 = await stale.post("/auth/refresh")
    assert r2.status_code == 401

    # семейство отозвано ЦЕЛИКОМ: даже последний легитимный токен больше не работает
    c.cookies.set("schoolhub_refresh", refresh2, path="/api/v1/auth")
    r3 = await c.post("/auth/refresh")
    assert r3.status_code == 401


async def test_refresh_grace_window_for_parallel_tabs(client, seeded):
    """Старый токен в пределах льготного окна (параллельная вкладка) — не кража."""
    c = new_client(client)
    _, refresh1 = await _login(c, "teacher1@example.com")

    r = await c.post("/auth/refresh")
    assert r.status_code == 200

    # вторая «вкладка» предъявляет старый токен СРАЗУ (окно 30 с по умолчанию)
    tab2 = new_client(client)
    tab2.cookies.set("schoolhub_refresh", refresh1, path="/api/v1/auth")
    r2 = await tab2.post("/auth/refresh")
    assert r2.status_code == 200, "льготное окно должно пропускать параллельную вкладку"


async def test_remember_false_sets_session_cookie(client, seeded):
    c = new_client(client)
    resp = await c.post(
        "/auth/login", json={"email": "teacher1@example.com", "password": DEMO_PASSWORD, "remember": False}
    )
    set_cookie = resp.headers.get("set-cookie", "")
    assert "max-age" not in set_cookie.lower() and "expires" not in set_cookie.lower()

    resp2 = await new_client(client).post(
        "/auth/login", json={"email": "teacher1@example.com", "password": DEMO_PASSWORD, "remember": True}
    )
    assert "max-age" in resp2.headers.get("set-cookie", "").lower()


# ----------------------------------------------------------------------- выход
async def test_logout_revokes_refresh(client, seeded):
    c = new_client(client)
    access, _ = await _login(c, "teacher1@example.com")
    c.headers["Authorization"] = f"Bearer {access}"

    assert (await c.post("/auth/logout")).status_code == 204

    # access доживает свои минуты, но refresh больше не работает
    assert (await c.get("/auth/me")).status_code == 200
    c2 = new_client(client)
    c2.cookies.set("schoolhub_refresh", c.cookies.get("schoolhub_refresh") or "", path="/api/v1/auth")
    r = await c.post("/auth/refresh")
    assert r.status_code == 401, "после выхода refresh-сессия обязана быть отозвана"


# ------------------------------------------- смена пароля и блокировка учётки
async def test_password_reset_revokes_all_sessions(client, seeded):
    c = new_client(client)
    access, _ = await _login(c, "teacher1@example.com")

    a = await auth(client, "admin@example.com")
    tid = seeded["teacher1@example.com"]
    resp = await a.patch(f"/users/{tid}", json={"password": "BrandNew123!"})
    assert resp.status_code == 200

    r = await c.post("/auth/refresh")
    assert r.status_code == 401, "сброс пароля обязан отозвать все сессии"

    # старый access тоже больше не пустит? access живёт до expiry, но вход по новому паролю работает
    c.headers["Authorization"] = f"Bearer {access}"
    assert (await c.get("/auth/me")).status_code == 200     # access доживает TTL (30 мин) — штатно
    fresh = new_client(client)
    resp2 = await fresh.post(
        "/auth/login", json={"email": "teacher1@example.com", "password": "BrandNew123!"}
    )
    assert resp2.status_code == 200


async def test_change_password_revokes_sessions(client, seeded):
    c = new_client(client)
    access, _ = await _login(c, "teacher1@example.com")
    c.headers["Authorization"] = f"Bearer {access}"

    resp = await c.post(
        "/auth/change-password", json={"current_password": DEMO_PASSWORD, "new_password": "SelfNew123!"}
    )
    assert resp.status_code == 204
    assert (await c.post("/auth/refresh")).status_code == 401


async def test_deactivation_blocks_access_and_refresh(client, seeded):
    from tests.test_totp_pep import sign

    cli, sig = await sign(client, "owner@example.com")
    owner = cli
    tid = seeded["teacher2@example.com"]

    c = new_client(client)
    access, _ = await _login(c, "teacher2@example.com")
    c.headers["Authorization"] = f"Bearer {access}"

    resp = await owner.request("DELETE", f"/users/{tid}", json=sig())
    assert resp.status_code == 204, resp.text

    # заблокированная учётка: access отклоняется СРАЗУ (проверка is_active на каждом запросе)
    assert (await c.get("/auth/me")).status_code == 403
    # refresh отозван
    assert (await c.post("/auth/refresh")).status_code in (401, 403)


# --------------------------------------------------------------- аудит и чистка
async def test_reuse_is_audited(client, seeded, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "refresh_reuse_grace_sec", 0)
    o = await auth(client, "owner@example.com")
    c = new_client(client)
    _, refresh1 = await _login(c, "teacher1@example.com")
    await c.post("/auth/refresh")            # ротируем

    stale = new_client(client)
    stale.cookies.set("schoolhub_refresh", refresh1, path="/api/v1/auth")
    await stale.post("/auth/refresh")        # reuse → отзыв семейства

    audit = (await o.get("/audit", params={"per_page": 100})).json()
    assert any(a["action"] == "TOKEN_REUSE_DETECTED" for a in audit["items"])
