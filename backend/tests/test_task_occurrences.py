"""История повторов повторяющихся задач (v1.10): task_occurrences.

Ключевые проверки:
  * next_period_start — чистая математика следующего периода (DAILY/WEEKLY/MONTHLY);
  * выполнение периода пишет строку DONE (кто закрыл, когда), бейдж-сводка на задаче;
  * истёкший невыполненный период закрывается rollover как MISSED, missed_streak растёт;
  * выполнение после провала сбрасывает missed_streak в 0;
  * снятие отметки «выполнено» откатывает DONE текущего периода (retract);
  * легаси-задачи (курсор NULL): отсчёт с сегодня, задним числом не проваливаем;
  * доступ: исполнитель видит историю, посторонний учитель — нет.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import update

from app.models.task import Task
from app.services.tasks import TaskRecurrence, next_period_start
from tests.conftest import auth

pytestmark = pytest.mark.asyncio


def task_payload(assignee_ids: list[str] | None = None, **kw) -> dict:
    base = {
        "title": "Ежедневный обход корпусов",
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


def test_next_period_start_daily():
    t = _FakeTask(TaskRecurrence.DAILY)
    start = date(2026, 10, 28)
    assert next_period_start(t, start) == date(2026, 10, 29)


def test_next_period_start_weekly():
    # 2026-10-26 — понедельник, 2026-10-28 — среда
    t = _FakeTask(TaskRecurrence.WEEKLY, weekdays=[1, 3])
    assert next_period_start(t, date(2026, 10, 26)) == date(2026, 10, 28)
    assert next_period_start(t, date(2026, 10, 28)) == date(2026, 11, 2)  # следующий понедельник


def test_next_period_start_monthly_clamps_short_months():
    t = _FakeTask(TaskRecurrence.MONTHLY, day=31)
    # 31 января → 28 февраля (2026 — не високосный) → 31 марта
    assert next_period_start(t, date(2026, 1, 31)) == date(2026, 2, 28)
    assert next_period_start(t, date(2026, 2, 28)) == date(2026, 3, 31)


# ------------------------------------------------------------- API: история
async def test_done_period_recorded(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    # свежая задача: истории закрытых периодов ещё нет, провалов нет
    assert task["missed_streak"] == 0
    assert (await a.get(f"/tasks/{tid}/occurrences")).json() == []

    t = await auth(client, "teacher1@example.com")
    await t.post(f"/tasks/{tid}/my-mark")

    occ = (await a.get(f"/tasks/{tid}/occurrences")).json()
    assert len(occ) == 1
    assert occ[0]["status"] == "DONE"
    assert occ[0]["period_start"] == date.today().isoformat()
    assert occ[0]["completed_at"] is not None
    assert len(occ[0]["completed_by"]) == 1 and occ[0]["completed_by"][0]

    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["effective_status"] == "DONE"
    assert detail["missed_streak"] == 0
    assert detail["last_occurrence_status"] == "DONE"
    assert detail["last_occurrence_start"] == date.today().isoformat()


async def test_missed_period_closed_by_rollover(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]
    yesterday = date.today() - timedelta(days=1)

    # симулируем «вчера период не выполнили»: курсор остался на вчерашнем дне
    await db_session.execute(update(Task).where(Task.id == tid).values(occurrence_cursor=yesterday))
    await db_session.commit()

    # любой GET задачи дёргает rollover_due — он и закрывает вчерашний период
    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["missed_streak"] == 1
    assert detail["last_occurrence_status"] == "MISSED"
    assert detail["last_occurrence_start"] == yesterday.isoformat()

    occ = (await a.get(f"/tasks/{tid}/occurrences")).json()
    assert len(occ) == 1
    assert occ[0]["status"] == "MISSED"
    assert occ[0]["period_start"] == yesterday.isoformat()
    assert occ[0]["completed_at"] is None
    assert occ[0]["completed_by"] == []


async def test_missed_streak_resets_after_done(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    # два проваленных дня подряд
    two_days_ago = date.today() - timedelta(days=2)
    await db_session.execute(update(Task).where(Task.id == tid).values(occurrence_cursor=two_days_ago))
    await db_session.commit()
    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["missed_streak"] == 2

    # сегодня выполнили — серия провалов обнуляется, провалы в истории остаются
    t = await auth(client, "teacher1@example.com")
    await t.post(f"/tasks/{tid}/my-mark")
    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["missed_streak"] == 0
    assert detail["last_occurrence_status"] == "DONE"

    occ = (await a.get(f"/tasks/{tid}/occurrences")).json()
    assert [o["status"] for o in occ] == ["DONE", "MISSED", "MISSED"]  # новые первыми


async def test_clear_mark_retracts_done_occurrence(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    t = await auth(client, "teacher1@example.com")
    await t.post(f"/tasks/{tid}/my-mark")
    assert len((await a.get(f"/tasks/{tid}/occurrences")).json()) == 1

    # сняли отметку — период снова открыт: строка DONE текущего периода откатывается
    await t.delete(f"/tasks/{tid}/my-mark")
    assert (await a.get(f"/tasks/{tid}/occurrences")).json() == []
    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["effective_status"] in ("NEW", "IN_PROGRESS")
    assert detail["last_occurrence_status"] is None


async def test_legacy_task_history_starts_today(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    # легаси-задача: курсор ещё не инициализирован (как до v1.10)
    await db_session.execute(update(Task).where(Task.id == tid).values(occurrence_cursor=None))
    await db_session.commit()

    detail = (await a.get(f"/tasks/{tid}")).json()
    assert detail["missed_streak"] == 0
    # задним числом ничего не провалено
    assert (await a.get(f"/tasks/{tid}/occurrences")).json() == []


async def test_occurrences_access_control(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    # исполнитель видит историю своей задачи
    t = await auth(client, "teacher1@example.com")
    assert (await t.get(f"/tasks/{tid}/occurrences")).status_code == 200

    # посторонний учитель — нет
    t2 = await auth(client, "teacher2@example.com")
    r = await t2.get(f"/tasks/{tid}/occurrences")
    assert r.status_code in (403, 404)


async def test_admin_status_done_records_occurrence(client, seeded):
    """Админ закрыл задачу статусом напрямую (без отметок) — период DONE за ним."""
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1], recurrence="DAILY"))).json()
    tid = task["id"]

    r = await a.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"})
    assert r.status_code == 200
    r = await a.post(f"/tasks/{tid}/status", json={"status": "DONE"})
    assert r.status_code == 200

    occ = (await a.get(f"/tasks/{tid}/occurrences")).json()
    assert len(occ) == 1 and occ[0]["status"] == "DONE"
    assert len(occ[0]["completed_by"]) == 1  # исполнитель без отметки — закрыл админ

    # возврат из DONE откатывает запись периода
    await a.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"})
    assert (await a.get(f"/tasks/{tid}/occurrences")).json() == []
