"""Повторяющиеся задачи: одна строка в БД, автосброс к новому периоду, вычисления.

Ключевые проверки:
  * периодичность сохраняется, дедлайн у повторяющихся задач не используется;
  * выполненная в текущем периоде задача — DONE, done_this_period=True;
  * после «полуночи» (сдвиг completed_at в прошлое) задача сама возвращается в NEW,
    отметки исполнителей очищаются, last_done_at сохраняется;
  * weekly-период: выполнение засчитывается до следующего дня повторения;
  * фильтр recurring и смена периодичности через PATCH.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.models.task import Task
from app.services.tasks import TaskRecurrence, period_start_date, validate_recurrence
from tests.conftest import auth

pytestmark = pytest.mark.asyncio


def task_payload(assignee_ids: list[str] | None = None, **kw) -> dict:
    base = {
        "title": "Полить цветы в кабинете 204",
        "assignee_ids": assignee_ids or [],
        "priority": "NORMAL",
    }
    base.update(kw)
    return base


# ------------------------------------------------- чистые вычисления периодов
class _FakeTask:
    def __init__(self, recurrence, weekdays=None, day=None):
        self.recurrence = recurrence
        self.recurrence_weekdays = weekdays
        self.recurrence_day = day


async def test_period_start_daily():
    t = _FakeTask(TaskRecurrence.DAILY)
    assert period_start_date(t, date(2026, 10, 13)) == date(2026, 10, 13)


async def test_period_start_weekly():
    t = _FakeTask(TaskRecurrence.WEEKLY, [1, 3, 5])  # пн, ср, пт
    assert period_start_date(t, date(2026, 10, 14)) == date(2026, 10, 14)  # среда — сегодня
    assert period_start_date(t, date(2026, 10, 18)) == date(2026, 10, 16)  # воскресенье → пятница


async def test_period_start_monthly_clamps_short_months():
    t = _FakeTask(TaskRecurrence.MONTHLY, day=29)
    assert period_start_date(t, date(2026, 2, 15)) == date(2026, 1, 29)  # до 29-го — прошлый месяц
    assert period_start_date(t, date(2026, 2, 28)) == date(2026, 2, 28)  # февраль: перенос на 28-е
    assert period_start_date(t, date(2026, 3, 1)) == date(2026, 2, 28)   # 1 марта — ещё февральский период


async def test_validate_recurrence_rejects_garbage():
    from app.errors import AppError

    with pytest.raises(AppError):
        validate_recurrence("HOURLY", None, None)
    with pytest.raises(AppError):
        validate_recurrence("WEEKLY", [], None)
    with pytest.raises(AppError):
        validate_recurrence("WEEKLY", [0, 8], None)
    with pytest.raises(AppError):
        validate_recurrence("MONTHLY", None, None)
    with pytest.raises(AppError):
        validate_recurrence("MONTHLY", None, 32)
    assert validate_recurrence("weekly", [3, 1, 1], None) == ("WEEKLY", [1, 3], None)
    assert validate_recurrence(None, None, None) == ("NONE", None, None)


# ---------------------------------------------------------------- API: создание
async def test_create_recurring_task_clears_deadline(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/tasks", json=task_payload(
        [seeded["teacher1@example.com"]],
        recurrence="DAILY",
        deadline=datetime.now(timezone.utc).astimezone().replace(hour=23, minute=0).isoformat(),
    ))
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["recurrence"] == "DAILY"
    assert task["deadline"] is None          # у повторяющихся задач дедлайна нет
    assert task["is_overdue"] is False       # и просрочки не бывает
    assert task["done_this_period"] is False


async def test_create_weekly_requires_weekdays(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]], recurrence="WEEKLY"))
    assert resp.status_code == 400
    assert "дни недели" in resp.json()["detail"]


# ------------------------------------------------------- API: цикл «выполнение → сброс»
async def test_daily_task_resets_next_day(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    # исполнитель отмечается — задача закрывается на сегодня
    t = await auth(client, "teacher1@example.com")
    marked = (await t.post(f"/tasks/{tid}/my-mark")).json()
    assert marked["status"] == "DONE"
    assert marked["effective_status"] == "DONE"
    assert marked["done_this_period"] is True
    assert marked["last_done_at"] is not None

    # выполненные сегодня не «воскресают»
    assert (await a.get(f"/tasks/{tid}")).json()["effective_status"] == "DONE"

    # «наступило завтра»: сдвигаем completed_at во вчерашний день
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.execute(update(Task).where(Task.id == tid).values(completed_at=yesterday))
    await db_session.commit()

    fresh = (await a.get(f"/tasks/{tid}")).json()
    assert fresh["status"] == "NEW"                  # автосброс выполнен
    assert fresh["effective_status"] == "NEW"
    assert fresh["done_this_period"] is False
    assert fresh["assignees"][0]["done_at"] is None  # отметка исполнителя очищена
    assert fresh["last_done_at"] is not None         # история «когда выполняли» сохранена

    # задача снова в активных, а не в выполненных
    active = (await a.get("/tasks", params={"status": "ACTIVE"})).json()
    assert any(x["id"] == tid for x in active["items"])
    assert active["status_counts"]["NEW"] >= 1


async def test_weekly_task_stays_done_until_next_occurrence(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    today = date.today()
    weekday = today.isoweekday()  # повторяем «каждую неделю» в сегодняшний день недели
    task = (await a.post("/tasks", json=task_payload(
        [seeded["teacher1@example.com"]], recurrence="WEEKLY", recurrence_weekdays=[weekday],
    ))).json()
    tid = task["id"]

    t = await auth(client, "teacher1@example.com")
    await t.post(f"/tasks/{tid}/my-mark")

    # выполнено вчера (день недели тот же, неделя прошлая) → начался новый период, сброс
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.execute(update(Task).where(Task.id == tid).values(completed_at=yesterday))
    await db_session.commit()
    assert (await a.get(f"/tasks/{tid}")).json()["effective_status"] == "NEW"

    # снова выполняем и проверяем, что в рамках текущей недели задача остаётся DONE
    await t.post(f"/tasks/{tid}/my-mark")
    done = (await a.get(f"/tasks/{tid}")).json()
    assert done["effective_status"] == "DONE"
    assert done["done_this_period"] is True


async def test_recurring_filter_and_update(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    rec = (await a.post("/tasks", json=task_payload([t1], recurrence="MONTHLY", recurrence_day=1))).json()
    plain = (await a.post("/tasks", json=task_payload([t1], title="Обычная задача без повторов"))).json()

    only_rec = (await a.get("/tasks", params={"recurring": "true", "status": "ACTIVE"})).json()
    ids = {x["id"] for x in only_rec["items"]}
    assert rec["id"] in ids and plain["id"] not in ids

    only_plain = (await a.get("/tasks", params={"recurring": "false", "status": "ACTIVE"})).json()
    ids = {x["id"] for x in only_plain["items"]}
    assert plain["id"] in ids and rec["id"] not in ids

    # PATCH: переводим ежемесячную в ежедневную — история содержит событие «Повторение:»
    updated = (await a.patch(f"/tasks/{rec['id']}", json={"recurrence": "DAILY"})).json()
    assert updated["recurrence"] == "DAILY"
    assert updated["recurrence_day"] is None
    detail = (await a.get(f"/tasks/{rec['id']}")).json()
    notes = [h.get("note") or "" for h in detail["history"]]
    assert any(n.startswith("Повторение:") for n in notes)


async def test_stale_marks_cleared_for_open_recurring_task(client, seeded, db_session):
    """Отметка прошлого периода у НЕзакрытой задачи не должна засчитываться в новом."""
    a = await auth(client, "admin@example.com")
    t1, t2 = seeded["teacher1@example.com"], seeded["teacher2@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1, t2], recurrence="DAILY"))).json()
    tid = task["id"]

    # отметился только первый исполнитель — задача «в работе»
    t = await auth(client, "teacher1@example.com")
    marked = (await t.post(f"/tasks/{tid}/my-mark")).json()
    assert marked["status"] == "IN_PROGRESS" and marked["marks_done"] == 1

    # «наступил следующий день»: отметка первого исполнителя устаревает
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    from app.models.task import TaskAssignee

    await db_session.execute(
        update(TaskAssignee)
        .where(TaskAssignee.task_id == tid)
        .values(done_at=yesterday)
    )
    await db_session.commit()

    fresh = (await a.get(f"/tasks/{tid}")).json()
    assert fresh["marks_done"] == 0                       # устаревшая отметка очищена
    assert all(x["done_at"] is None for x in fresh["assignees"])

    # теперь, чтобы задача закрылась, отметиться должны ОБА исполнителя заново
    await t.post(f"/tasks/{tid}/my-mark")
    after_one = (await a.get(f"/tasks/{tid}")).json()
    assert after_one["status"] == "IN_PROGRESS" and after_one["marks_done"] == 1
