"""Модуль «План работы на неделю»: CRUD, жизненный цикл, RBAC, уведомления, дашборд."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import auth

pytestmark = pytest.mark.asyncio


def monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def week_days(start: date) -> list[str]:
    return [str(start + timedelta(days=i)) for i in range(6)]


def plan_payload(week_start: date | None = None, *, with_events: bool = True, resp: list[str] | None = None) -> dict:
    ws = week_start or monday()
    resp = resp or []
    events = (
        [
            {"title": "«Разговоры о важном».", "time": "08.00", "place": "Кл. каб.", "resp": resp},
            {"title": "Совещание при директоре.", "time": "15.00", "place": "ЦПД", "resp": resp},
        ]
        if with_events else []
    )
    days = [{"date": d, "events": events if i == 0 else []} for i, d in enumerate(week_days(ws))]
    return {
        "week_start": str(ws),
        "content": {
            "duty": {"klass": "9 «Б»", "teacher_id": resp[0] if resp else None},
            "notes": ["В столовой дежурят 7-ые классы."],
            "month_items": [{"text": "Контроль адаптации 1-х классов.", "resp": resp}],
            "week_items": [{"text": "Подготовка к ВсОШ.", "place": "Кабинеты", "resp": resp}],
            "days": days,
        },
    }


# ------------------------------------------------------------------ создание
async def test_teacher_cannot_manage_plans(client, seeded):
    t = await auth(client, "teacher1@example.com")
    assert (await t.post("/plans", json=plan_payload())).status_code == 403
    assert (await t.get("/plans")).status_code == 200      # читать опубликованные — можно


async def test_admin_creates_draft_plan(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/plans", json=plan_payload())
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["status"] == "DRAFT"
    assert plan["week_start"] == str(monday())
    assert plan["week_end"] == str(monday() + timedelta(days=5))
    assert plan["events_total"] == 2
    assert plan["can_manage"] is True
    assert plan["content"]["duty"]["klass"] == "9 «Б»"
    assert plan["content"]["days"][0]["weekday"] == "понедельник"
    assert plan["content"]["days"][4]["weekday"] == "пятница"


async def test_week_must_start_on_monday(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/plans", json=plan_payload(monday() + timedelta(days=2)))
    assert resp.status_code == 422          # валидация Pydantic


async def test_duplicate_week_rejected(client, seeded):
    a = await auth(client, "admin@example.com")
    assert (await a.post("/plans", json=plan_payload())).status_code == 201
    assert (await a.post("/plans", json=plan_payload())).status_code == 409


async def test_unknown_responsible_rejected(client, seeded):
    a = await auth(client, "admin@example.com")
    payload = plan_payload(resp=["00000000-0000-0000-0000-000000000009"])
    assert (await a.post("/plans", json=payload)).status_code == 400


# ----------------------------------------------------------- жизненный цикл
async def test_publish_notifies_responsibles_and_visibility(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    plan = (await a.post("/plans", json=plan_payload(resp=[t1]))).json()
    pid = plan["id"]

    # черновик не виден учителю
    t = await auth(client, "teacher1@example.com")
    assert (await t.get(f"/plans/{pid}")).status_code == 403
    listing = (await t.get("/plans")).json()
    assert all(p["id"] != pid for p in listing["items"])

    # пустой план не публикуется
    empty_plan = (await a.post("/plans", json=plan_payload(monday() + timedelta(days=7), with_events=False))).json()
    empty_plan["content"]["month_items"] = []
    empty_plan["content"]["week_items"] = []
    await a.patch(f"/plans/{empty_plan['id']}", json={"content": empty_plan["content"]})
    assert (await a.post(f"/plans/{empty_plan['id']}/publish")).status_code == 400

    # публикация
    resp = await a.post(f"/plans/{pid}/publish")
    assert resp.status_code == 200
    published = resp.json()
    assert published["status"] == "PUBLISHED" and published["published_at"]
    assert published["my_items"] == 0                     # админ не ответственный
    assert published["responsibles_total"] == 1

    # ответственный получил уведомление и видит план
    notes = (await t.get("/notifications")).json()["items"]
    assert any(n["type"] == "PLAN_PUBLISHED" and n["entity_id"] == pid for n in notes)
    assert (await t.get(f"/plans/{pid}")).status_code == 200
    mine = (await t.get(f"/plans/{pid}")).json()
    assert mine["my_items"] == 5                          # дежурный + месяц + неделя + 2 мероприятия
    assert mine["can_manage"] is False

    # повторная публикация — 409
    assert (await a.post(f"/plans/{pid}/publish")).status_code == 409

    # current + дашборд-виджет
    current = (await t.get("/plans/current")).json()
    assert current["id"] == pid
    dash = (await t.get("/dashboard")).json()
    assert dash["current_plan"]["id"] == pid

    # архив
    resp = await a.post(f"/plans/{pid}/archive")
    assert resp.status_code == 200 and resp.json()["status"] == "ARCHIVED"
    # архив виден учителю, но не редактируется
    assert (await t.get(f"/plans/{pid}")).status_code == 200
    fresh_content = plan_payload(resp=[t1])["content"]
    assert (await a.patch(f"/plans/{pid}", json={"content": fresh_content})).status_code == 409


async def test_draft_delete_and_edit(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    plan = (await a.post("/plans", json=plan_payload(resp=[t1]))).json()
    pid = plan["id"]

    # редактирование: смена недели и контента
    next_week = monday() + timedelta(days=7)
    content = plan_payload(next_week, resp=[t1])["content"]
    for day in content["days"]:
        day["events"] = []
    resp = await a.patch(f"/plans/{pid}", json={"week_start": str(next_week), "content": content})
    assert resp.status_code == 200
    assert resp.json()["week_start"] == str(next_week)
    assert resp.json()["week_end"] == str(next_week + timedelta(days=5))
    assert resp.json()["events_total"] == 0

    # учитель не может удалить
    t = await auth(client, "teacher1@example.com")
    assert (await t.delete(f"/plans/{pid}")).status_code == 403

    # удаление черновика
    assert (await a.delete(f"/plans/{pid}")).status_code == 204
    assert (await a.get(f"/plans/{pid}")).status_code == 404


async def test_plan_list_filters_and_pagination(client, seeded):
    a = await auth(client, "admin@example.com")
    p1 = (await a.post("/plans", json=plan_payload(monday()))).json()
    await a.post(f"/plans/{p1['id']}/publish")
    await a.post("/plans", json=plan_payload(monday() - timedelta(days=7)))

    listing = (await a.get("/plans")).json()
    assert listing["total"] == 2
    # сортировка: свежие недели первыми
    assert listing["items"][0]["week_start"] > listing["items"][1]["week_start"]

    published = (await a.get("/plans", params={"status": "PUBLISHED"})).json()
    assert published["total"] == 1

    t = await auth(client, "teacher1@example.com")
    teacher_listing = (await t.get("/plans")).json()
    assert teacher_listing["total"] == 1          # черновик скрыт
    assert (await t.get("/plans", params={"status": "DRAFT"})).json()["total"] == 0


async def test_owner_can_delete_archived_plan(client, seeded):
    a = await auth(client, "admin@example.com")
    o = await auth(client, "owner@example.com")
    t = await auth(client, "teacher1@example.com")

    plan = (await a.post("/plans", json=plan_payload())).json()
    pid = plan["id"]
    assert (await a.post(f"/plans/{pid}/publish")).status_code == 200

    # опубликованный план не удаляет даже Владелец
    assert (await o.delete(f"/plans/{pid}")).status_code == 409

    assert (await a.post(f"/plans/{pid}/archive")).status_code == 200

    # архивный: администрация и учитель — нет, Владелец — да
    assert (await a.delete(f"/plans/{pid}")).status_code == 403
    assert (await t.delete(f"/plans/{pid}")).status_code == 403
    assert (await o.delete(f"/plans/{pid}")).status_code == 204
    assert (await o.get(f"/plans/{pid}")).status_code == 404
