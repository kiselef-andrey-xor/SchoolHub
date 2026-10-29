"""Заголовки безопасности ответов API и защита от «CORS_ORIGINS=*».

Проверяем то, что нельзя увидеть глазами в браузере, но что ломает защиту целиком:

* у каждого ответа API есть `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy` и строгая CSP по умолчанию (выгрузка, открытая напрямую,
  не должна уметь выполнять скрипты);
* печатные формы сохраняют СВОЮ CSP (им нужны inline-стили и автопечать) —
  middleware работает через setdefault и не затирает их;
* «звёздочка» в `CORS_ORIGINS` игнорируется: вместе с `allow_credentials=True`
  она открыла бы ответы API любому сайту.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import create_app
from tests.conftest import auth
from tests.test_service_notes import draft

pytestmark = pytest.mark.asyncio

API_CSP = "default-src 'none'"


async def test_api_responses_carry_security_headers(client):
    resp = await client.get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert API_CSP in csp
    assert "frame-ancestors 'none'" in csp
    # никаких «слабых» разрешений в базовой политике API
    assert "unsafe-inline" not in csp
    assert "*" not in csp


async def test_print_form_keeps_its_own_csp(client, seeded):
    """Печатная форма служебной записки: своя CSP важнее базовой."""
    teacher, note = await draft(client, executor_id=seeded["teacher_2"], subject="О печати")
    resp = await teacher.get(f"/notes/{note['id']}/print")
    assert resp.status_code == 200, resp.text
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline'" in csp, csp
    assert "script-src 'unsafe-inline'" in csp, csp       # автопечать onload=window.print()
    assert "default-src 'none'" in csp
    # заголовок выдаёт сама форма, а не middleware: frame-ancestors базовой политики не подмешан
    assert "frame-ancestors" not in csp


async def test_cors_wildcard_is_ignored(monkeypatch, session_factory):
    """`CORS_ORIGINS=*` + allow_credentials=True — дыра; «звёздочка» отбрасывается."""
    monkeypatch.setattr(settings, "cors_origins", ["*", " https://ok.example ", ""])
    app = create_app()
    app.state.setup_complete = True
    app.state.session_factory = session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        evil = await c.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in evil.headers, evil.headers

        ok = await c.get("/health", headers={"Origin": "https://ok.example"})
        assert ok.headers.get("access-control-allow-origin") == "https://ok.example"
        assert ok.headers.get("access-control-allow-credentials") == "true"

        # предзапрос от чужого сайта тоже не проходит
        pre = await c.options(
            "/health",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in pre.headers


async def test_cors_explicit_origin_still_works(client):
    """Явно перечисленные адреса (как в dev-сборке) продолжают работать."""
    allowed = settings.cors_origins[0]
    resp = await client.get("/health", headers={"Origin": allowed})
    assert resp.headers.get("access-control-allow-origin") == allowed


async def test_owner_endpoints_are_not_framable(client, seeded):
    """Ответ с персональными данными нельзя встроить в iframe чужого сайта."""
    owner = await auth(client, "owner@example.com")
    resp = await owner.get("/users")
    assert resp.status_code == 200
    assert resp.headers["x-frame-options"] == "DENY"
    assert API_CSP in resp.headers["content-security-policy"]
