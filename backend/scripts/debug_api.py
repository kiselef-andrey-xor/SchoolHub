"""Отладочный стенд: поднимает приложение на in-memory SQLite и дёргает API.

Не является частью боевого кода — используется для ручной проверки сценариев.
Запуск:  python scripts/debug_api.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("UPLOAD_DIR", "/tmp/schoolhub-debug-uploads")
os.environ.setdefault("SECRET_KEY", "debug-secret-key-not-for-production-32ch")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base, get_session  # noqa: E402
from app.enums import Role  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.user import User  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "Test1234!"
PEOPLE = [("o@ex.com", Role.OWNER), ("a@ex.com", Role.ADMIN), ("t1@ex.com", Role.TEACHER), ("t2@ex.com", Role.TEACHER)]


def pdf(text: str = "debug") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 760, text)
    c.showPage()
    c.save()
    return buf.getvalue()


async def main() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SF = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with SF() as s:
        for email, role in PEOPLE:
            s.add(User(email=email, password_hash=hash_password(PASSWORD), full_name=email, role=role, is_active=True))
        await s.commit()

    app = create_app()

    async def override():
        async with SF() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = override

    async def client_for(email: str) -> AsyncClient:
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://debug/api/v1")
        r = await c.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        return c

    owner = await client_for("o@ex.com")
    admin = await client_for("a@ex.com")
    teacher = await client_for("t1@ex.com")

    def show(label, resp):
        body = resp.text
        print(f"{label:<42} {resp.status_code}  {body[:220]}")

    show("GET /audit (owner)", await owner.get("/audit"))
    show("GET /audit/actions (owner)", await owner.get("/audit/actions"))
    show("GET /meta/enums", (await owner.get("/meta/enums")))
    show("GET /meta/permissions", await owner.get("/meta/permissions"))

    from datetime import date

    order = (
        await admin.post(
            "/orders",
            json={"number": "1-к", "order_year": date.today().year, "title": "Отладочный приказ",
                  "order_type": "HR", "issued_on": date.today().isoformat()},
        )
    ).json()
    show("POST /orders/{id}/files", await admin.post(
        f"/orders/{order['id']}/files", files={"file": ("p.pdf", io.BytesIO(pdf()), "application/pdf")}))
    show("POST /orders/{id}/publish", await admin.post(f"/orders/{order['id']}/publish"))
    show("GET /orders (teacher)", await teacher.get("/orders"))
    show("POST ack (teacher)", await teacher.post(f"/orders/{order['id']}/acknowledgements", json={"comment": "ок"}))
    show("GET /dashboard (teacher)", await teacher.get("/dashboard"))
    show("GET /dashboard (owner)", await owner.get("/dashboard"))
    show("GET /orders/{id}/preview", await teacher.get(f"/orders/{order['id']}/preview"))

    task = (
        await admin.post(
            "/tasks",
            json={"title": "Отладочная задача", "priority": "HIGH",
                  "assignee_id": (await teacher.get("/auth/me")).json()["id"]},
        )
    )
    show("POST /tasks (admin)", task)


if __name__ == "__main__":
    asyncio.run(main())
