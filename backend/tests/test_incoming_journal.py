"""Журнал входящих писем: авто-записи из задач, грант доступа, экспорт, печать-данные."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth

pytestmark = pytest.mark.asyncio


def task_payload(incoming_number: str | None, assignee_ids: list[str]) -> dict:
    return {
        "title": "Разобрать письмо министерства",
        "description": None,
        "assignee_ids": assignee_ids,
        "deadline": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "priority": "NORMAL",
        "incoming_number": incoming_number,
    }


async def test_task_incoming_number_creates_journal_row(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")
    teacher = await auth(client, "teacher1@example.com")

    resp = await admin.post("/tasks", json=task_payload("01-15/234", [seeded["teacher1@example.com"]]))
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    # доступ: владелец — да, админ без гранта — нет, учитель — нет
    assert (await owner.get("/incoming")).status_code == 200
    assert (await admin.get("/incoming")).status_code == 403
    assert (await teacher.get("/incoming")).status_code == 403

    rows = (await owner.get("/incoming")).json()["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["incoming_number"] == "01-15/234"
    assert row["subject"] == "Разобрать письмо министерства"
    assert row["responsible"]["id"] == seeded["teacher1@example.com"]
    assert row["task_id"] == task_id
    assert row["reg_no"] == 1 and row["reg_year"] == datetime.now(timezone.utc).year

    # смена номера в задаче синхронизирует журнал
    resp = await admin.patch(f"/tasks/{task_id}", json={"incoming_number": "01-15/999"})
    assert resp.status_code == 200
    rows = (await owner.get("/incoming")).json()["items"]
    assert rows[0]["incoming_number"] == "01-15/999"

    # уборка номера удаляет запись журнала
    resp = await admin.patch(f"/tasks/{task_id}", json={"incoming_number": None})
    assert resp.status_code == 200
    assert (await owner.get("/incoming")).json()["total"] == 0


async def test_grant_export_manual_and_delete(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")
    aid = seeded["admin@example.com"]

    # владелец выдаёт грант админу через карточку пользователя
    resp = await owner.patch(f"/users/{aid}", json={"incoming_journal_access": True})
    assert resp.status_code == 200
    assert resp.json()["incoming_journal_access"] is True
    # админ не может выдать грант сам себе или другим
    resp = await admin.patch(f"/users/{aid}", json={"incoming_journal_access": False})
    assert resp.status_code == 403

    # ручная запись
    resp = await admin.post(
        "/incoming",
        json={
            "incoming_number": "Вх-42",
            "subject": "Письмо о проверке",
            "addressee": "Директору",
            "sent_date": "2026-10-01",
            "note": "ответ до конца месяца",
            "responsible_id": seeded["owner@example.com"],
        },
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["reg_no"] == 1

    # правка ручных полей
    resp = await admin.patch(f"/incoming/{row['id']}", json={"sent_date": "2026-10-02", "note": "продлено"})
    assert resp.status_code == 200
    assert resp.json()["sent_date"] == "2026-10-02"

    # экспорт CSV
    resp = await admin.get("/incoming/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "№ регистрации" in body and "Вх-42" in body

    # удаление: только владелец
    assert (await admin.delete(f"/incoming/{row['id']}")).status_code == 403
    assert (await owner.delete(f"/incoming/{row['id']}")).status_code == 204
    assert (await owner.get("/incoming")).json()["total"] == 0


async def test_manual_reg_survives_cross_year_move(client, seeded):
    """Правка: перенос даты в другой год + ручной № регистрации одним запросом —
    ручной номер применяется к итоговому году, а не затирается авто-номером."""
    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/incoming",
        json={"incoming_number": "01-15/500", "incoming_date": "2025-03-03", "subject": "Письмо из 2025 года"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["reg_year"] == 2025 and row["reg_no"] == 1

    resp = await owner.patch(
        f"/incoming/{row['id']}", json={"incoming_date": "2024-05-06", "reg_no": 77}
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["reg_year"] == 2024 and row["reg_no"] == 77

    # занятый номер в итоговом году — 409
    resp2 = await owner.post(
        "/incoming",
        json={"incoming_number": "01-15/501", "incoming_date": "2024-05-07", "subject": "Ещё письмо в 2024"},
    )
    assert resp2.status_code == 201
    resp3 = await owner.patch(f"/incoming/{resp2.json()['id']}", json={"reg_no": 77})
    assert resp3.status_code == 409
