"""Пакет правок дашборда/задач:

  * лента «Все задачи» без выполненных (status=ACTIVE) + счётчик ACTIVE;
  * ручной «номер регистрации» задачи → журнал входящих (дубликат → 409);
  * дашборд «по школе»: незавершённые задачи видны даже без просрочек (регрессия);
  * виджет «мои мероприятия» из плана текущей недели;
  * POST /system/restore — доступ только Владельца, на sqlite — понятная 400.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import auth, upload

pytestmark = pytest.mark.asyncio


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def task_payload(assignee_ids: list[str] | None, **kw) -> dict:
    base = {
        "title": "Проверить тетради 9 «А»",
        "assignee_ids": assignee_ids or [],
        "deadline": iso(datetime.now(timezone.utc) + timedelta(days=3)),
        "priority": "NORMAL",
    }
    base.update(kw)
    return base


def monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def plan_payload(resp: list[str]) -> dict:
    ws = monday()
    days = [str(ws + timedelta(days=i)) for i in range(6)]
    events = [{"title": "«Разговоры о важном».", "time": "08.00", "place": "Кл. каб.", "resp": resp}]
    return {
        "week_start": str(ws),
        "content": {
            "duty": {"klass": "9 «Б»", "teacher_id": resp[0] if resp else None},
            "notes": [],
            "month_items": [{"text": "Контроль адаптации 1-х классов.", "resp": resp}],
            "week_items": [{"text": "Подготовка к ВсОШ.", "place": "Кабинеты", "resp": resp}],
            "days": [{"date": d, "events": events if i == 0 else []} for i, d in enumerate(days)],
        },
    }


# ------------------------------------------------------- лента «Все» без DONE
async def test_tasks_active_filter_hides_done(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    active = (await a.post("/tasks", json=task_payload([t1], title="Активная задача"))).json()
    done = (await a.post("/tasks", json=task_payload([t1], title="Выполненная задача"))).json()
    await a.post(f"/tasks/{done['id']}/status", json={"status": "DONE"})

    listing = (await a.get("/tasks", params={"status": "ACTIVE"})).json()
    ids = {t["id"] for t in listing["items"]}
    assert active["id"] in ids
    assert done["id"] not in ids
    # счётчик ACTIVE = все минус выполненные
    counts = listing["status_counts"]
    assert counts["ACTIVE"] == counts["all"] - counts["DONE"]

    # вкладка «Выполнены» по-прежнему отдаёт DONE
    done_list = (await a.get("/tasks", params={"status": "DONE"})).json()
    assert done["id"] in {t["id"] for t in done_list["items"]}

    # без фильтра — по-прежнему всё подряд (API не сломан)
    everything = (await a.get("/tasks")).json()
    assert {active["id"], done["id"]} <= {t["id"] for t in everything["items"]}


# --------------------------------------------------- ручной номер регистрации
async def test_task_manual_reg_no(client, seeded):
    owner = await auth(client, "owner@example.com")
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    year = date.today().year

    # ручной номер попадает в журнал входящих
    task = (
        await a.post("/tasks", json=task_payload([t1], title="Письмо Минобра", incoming_number="01-15/234", reg_no=42))
    ).json()
    detail = (await a.get(f"/tasks/{task['id']}")).json()
    assert detail["incoming_reg"] == f"42/{year}"

    reg = (await owner.get("/incoming", params={"search": "01-15/234"})).json()
    row = next(x for x in reg["items"] if x["task_id"] == task["id"])
    assert row["reg_no"] == 42 and row["reg_year"] == year

    # занятый номер → 409 с подсказкой
    dup = await a.post("/tasks", json=task_payload([t1], title="Дубль", incoming_number="01-15/999", reg_no=42))
    assert dup.status_code == 409
    assert "42" in dup.json()["detail"]

    # пусто — авто-номер по порядку (max+1 = 43)
    auto = (await a.post("/tasks", json=task_payload([t1], title="Авто-номер", incoming_number="01-15/235"))).json()
    auto_detail = (await a.get(f"/tasks/{auto['id']}")).json()
    assert auto_detail["incoming_reg"] == f"43/{year}"

    # обновление: смена номера и конфликт
    upd = await a.patch(f"/tasks/{auto['id']}", json={"reg_no": 100})
    assert upd.status_code == 200, upd.text
    assert (await a.get(f"/tasks/{auto['id']}")).json()["incoming_reg"] == f"100/{year}"
    assert (await a.patch(f"/tasks/{auto['id']}", json={"reg_no": 42})).status_code == 409


async def test_reg_no_ignored_without_incoming_number(client, seeded):
    """Без входящего номера запись журнала не создаётся — reg_no просто игнорируется."""
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], title="Без письма", reg_no=7))).json()
    assert task["incoming_number"] is None
    assert (await a.get(f"/tasks/{task['id']}")).json()["incoming_reg"] is None


# --------------------------------------- дашборд «по школе»: задачи без просрочек
async def test_dashboard_school_scope_shows_active_tasks(client, seeded):
    """Регрессия: раньше «по школе» показывались только OVERDUE — модуль пустовал."""
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], title="Свежая задача без просрочки"))).json()

    dash = (await a.get("/dashboard", params={"scope": "school"})).json()
    assert dash["summary"]["scope"] == "global"
    assert task["id"] in {t["id"] for t in dash["my_tasks"]}

    # выполненные в модуль не попадают
    await a.post(f"/tasks/{task['id']}/status", json={"status": "DONE"})
    dash = (await a.get("/dashboard", params={"scope": "school"})).json()
    assert task["id"] not in {t["id"] for t in dash["my_tasks"]}


async def test_dashboard_personal_scope_active_tasks(client, seeded):
    a = await auth(client, "admin@example.com")
    t = await auth(client, "teacher1@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], title="Личная задача учителя"))).json()

    dash = (await t.get("/dashboard", params={"scope": "mine"})).json()
    assert dash["summary"]["scope"] == "personal"
    assert task["id"] in {x["id"] for x in dash["my_tasks"]}

    await a.post(f"/tasks/{task['id']}/status", json={"status": "DONE"})
    dash = (await t.get("/dashboard", params={"scope": "mine"})).json()
    assert task["id"] not in {x["id"] for x in dash["my_tasks"]}


# ------------------------------------------------ мои мероприятия из плана
async def test_dashboard_my_plan_events(client, seeded):
    a = await auth(client, "admin@example.com")
    t = await auth(client, "teacher1@example.com")
    other = await auth(client, "teacher2@example.com")
    t1 = seeded["teacher1@example.com"]

    plan = (await a.post("/plans", json=plan_payload([t1]))).json()
    await a.post(f"/plans/{plan['id']}/publish")

    dash = (await t.get("/dashboard")).json()
    events = dash["my_plan_events"]
    kinds = [e["kind"] for e in events]
    # дежурство + мероприятие понедельника + недельный + месячный пункт
    assert kinds.count("duty") == 1
    assert kinds.count("event") == 1
    assert kinds.count("week") == 1
    assert kinds.count("month") == 1
    ev = next(e for e in events if e["kind"] == "event")
    assert ev["title"] == "«Разговоры о важном»."
    assert ev["date"] == str(monday()) and ev["weekday"] == "понедельник"
    assert ev["time"] == "08.00" and ev["place"] == "Кл. каб."
    duty = next(e for e in events if e["kind"] == "duty")
    assert "9 «Б»" in duty["title"]

    # у непричастного учителя список пуст, но план на дашборде остаётся
    dash_other = (await other.get("/dashboard")).json()
    assert dash_other["my_plan_events"] == []
    assert dash_other["current_plan"] is not None


# ---------------------------------------------------- восстановление из копии
async def test_restore_owner_only(client, seeded):
    payload = upload("copy.sql", b"-- PostgreSQL database dump\n", "application/sql")
    teacher = await auth(client, "teacher1@example.com")
    assert (await teacher.post("/system/restore", files=payload)).status_code == 403
    admin = await auth(client, "admin@example.com")
    assert (await admin.post("/system/restore", files=payload)).status_code == 403


async def test_restore_non_postgres_gets_clear_400(client, seeded):
    """Тестовая БД — sqlite: ждём человекочитаемую 400 (как у /system/backup)."""
    owner = await auth(client, "owner@example.com")
    payload = upload("copy.sql", b"-- PostgreSQL database dump\n", "application/sql")
    resp = await owner.post("/system/restore", files=payload)
    assert resp.status_code == 400, resp.text
    assert "PostgreSQL" in resp.json()["detail"]


async def test_dashboard_my_plan_events_not_from_stale_plan(client, seeded):
    """План ПРОШЛОЙ недели не должен выдаваться за «мероприятия на этой неделе»."""
    a = await auth(client, "admin@example.com")
    t = await auth(client, "teacher1@example.com")
    t1 = seeded["teacher1@example.com"]

    ws = monday() - timedelta(days=7)
    days = [str(ws + timedelta(days=i)) for i in range(6)]
    events = [{"title": "Мероприятие прошедшей недели.", "time": "09.00", "place": "Актовый зал", "resp": [t1]}]
    payload = {
        "week_start": str(ws),
        "content": {
            "duty": {"klass": "9 «Б»", "teacher_id": t1},
            "notes": [],
            "month_items": [],
            "week_items": [{"text": "Пункт прошедшей недели.", "resp": [t1]}],
            "days": [{"date": d, "events": events if i == 0 else []} for i, d in enumerate(days)],
        },
    }
    plan = (await a.post("/plans", json=payload)).json()
    assert plan.get("id"), plan
    await a.post(f"/plans/{plan['id']}/publish")

    dash = (await t.get("/dashboard")).json()
    # сам план как «последний опубликованный» на дашборде остаётся…
    assert dash["current_plan"] is not None and dash["current_plan"]["id"] == plan["id"]
    # …но мероприятий «на этой неделе» из просроченного плана нет
    assert dash["my_plan_events"] == []
