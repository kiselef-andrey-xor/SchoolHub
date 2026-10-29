"""Глобальный поиск (Ctrl+K) и отчёт «Итоги месяца» для администрации."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio

TODAY = date.today()
MONTH = f"{TODAY.year:04d}-{TODAY.month:02d}"


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def order_payload(**kw) -> dict:
    base = {
        "number": "777-к",
        "order_year": TODAY.year,
        "title": "Приказ о подведении итогов четверти",
        "description": None,
        "order_type": "HR",
        "issued_on": TODAY.isoformat(),
        "ack_deadline": (TODAY + timedelta(days=7)).isoformat(),
        "require_all": True,
    }
    base.update(kw)
    return base


def task_payload(assignee_ids: list[str], **kw) -> dict:
    base = {
        "title": "Проверить тетради 9 «А»",
        "assignee_ids": assignee_ids,
        "deadline": iso(datetime.now(timezone.utc) + timedelta(days=3)),
        "priority": "NORMAL",
    }
    base.update(kw)
    return base


async def test_search_sections_and_visibility(client, seeded):
    a = await auth(client, "admin@example.com")
    t = await auth(client, "teacher1@example.com")
    t1 = seeded["teacher1@example.com"]

    order = (await a.post("/orders", json=order_payload())).json()
    await a.post(f"/orders/{order['id']}/publish")
    task = (await a.post("/tasks", json=task_payload([t1], title="Уникальная формулировка проверки"))).json()

    # администрация видит все секции
    res = (await a.get("/search", params={"q": "Уникальная формулировка"})).json()
    assert [x["id"] for x in res["tasks"]] == [task["id"]]
    assert res["orders"] == [] and res["incoming"] == [] and res["users"] == []

    res = (await a.get("/search", params={"q": "777-к"})).json()
    assert res["orders"] and res["orders"][0]["id"] == order["id"]
    assert res["orders"][0]["url"] == f"/orders/{order['id']}"

    res = (await a.get("/search", params={"q": "Учитель Перв"})).json()
    assert any(u["id"] == t1 for u in res["users"])

    # учитель: задачи — только свои, людей нет вообще
    res = (await t.get("/search", params={"q": "Уникальная формулировка"})).json()
    assert [x["id"] for x in res["tasks"]] == [task["id"]]
    assert res["users"] == []
    res = (await t.get("/search", params={"q": "Учитель Перв"})).json()
    assert res["tasks"] == [] and res["users"] == []

    # черновик учителю в поиске не виден
    draft = (await a.post("/orders", json=order_payload(number="778-к", title="Черновик для поиска"))).json()
    res = (await t.get("/search", params={"q": "Черновик для поиска"})).json()
    assert res["orders"] == []
    res = (await a.get("/search", params={"q": "Черновик для поиска"})).json()
    assert [x["id"] for x in res["orders"]] == [draft["id"]]

    # слишком короткий запрос — 422
    assert (await a.get("/search", params={"q": "х"})).status_code == 422


async def test_month_summary_counts_and_access(client, seeded):
    a = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")
    t = await auth(client, "teacher1@example.com")
    t1 = seeded["teacher1@example.com"]

    # учителю отчёт недоступен
    assert (await t.get("/dashboard/month-summary", params={"month": MONTH})).status_code == 403
    # кривой месяц — 422
    assert (await a.get("/dashboard/month-summary", params={"month": "2026-13"})).status_code == 422
    assert (await a.get("/dashboard/month-summary", params={"month": "октябрь"})).status_code == 422

    order = (await a.post("/orders", json=order_payload(number="779-к", require_all=False, recipient_ids=[t1]))).json()
    resp = await a.post(
        f"/orders/{order['id']}/files", files=upload("p779.pdf", pdf_bytes("Приказ 779"), "application/pdf")
    )
    assert resp.status_code == 201, resp.text
    resp = await a.post(f"/orders/{order['id']}/publish")
    assert resp.status_code == 200, resp.text
    task = (await a.post("/tasks", json=task_payload([t1]))).json()
    await a.post(f"/tasks/{task['id']}/status", json={"status": "DONE"})

    rep = (await a.get("/dashboard/month-summary", params={"month": MONTH})).json()
    assert rep["month"] == MONTH
    assert rep["orders_published"] == 1
    assert rep["orders_by_type"] == [{"type": "HR", "count": 1}]
    assert rep["orders_ack_complete"] == 0          # никто ещё не ознакомился
    assert rep["tasks_created"] >= 1
    assert rep["tasks_done"] >= 1
    assert rep["tasks_overdue"] == 0

    # ознакомимся — приказ станет «полностью ознакомлен»
    await t.post(f"/orders/{order['id']}/acknowledgements", json={"comment": None})
    rep = (await owner.get("/dashboard/month-summary", params={"month": MONTH})).json()
    assert rep["orders_ack_complete"] == 1

    # пустой месяц прошлого года
    rep = (await a.get("/dashboard/month-summary", params={"month": "2020-01"})).json()
    assert rep["orders_published"] == 0 and rep["tasks_created"] == 0
