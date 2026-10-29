"""Task Manager: несколько исполнителей, отметки «выполнено», вложения, статусы (OVERDUE), RBAC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def task_payload(assignee_ids: list[str] | None, **kw) -> dict:
    base = {
        "title": "Проверить тетради 9 «А»",
        "description": "До понедельника, оценки в журнал",
        "assignee_ids": assignee_ids or [],
        "deadline": iso(datetime.now(timezone.utc) + timedelta(days=3)),
        "priority": "NORMAL",
    }
    base.update(kw)
    return base


def emails_of(task: dict) -> list[str]:
    return sorted(a["user"]["email"] for a in task["assignees"])


# ------------------------------------------------------------------- создание
async def test_teacher_cannot_create_tasks(client, seeded):
    t = await auth(client, "teacher1@example.com")
    resp = await t.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))
    assert resp.status_code == 403


async def test_admin_creates_task_and_assignees_get_notifications(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/tasks", json=task_payload(
        [seeded["teacher1@example.com"], seeded["teacher2@example.com"]], priority="HIGH"))
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "NEW"
    assert task["effective_status"] == "NEW"
    assert emails_of(task) == ["teacher1@example.com", "teacher2@example.com"]
    assert task["creator"]["email"] == "admin@example.com"
    assert task["is_overdue"] is False
    assert task["days_left"] in (2, 3)
    assert task["can_edit"] is True
    assert task["marks_done"] == 0

    t = await auth(client, "teacher1@example.com")
    notes = (await t.get("/notifications")).json()["items"]
    assigned = [n for n in notes if n["type"] == "TASK_ASSIGNED" and n["entity_id"] == task["id"]]
    assert assigned and "исполнителей: 2" in assigned[0]["body"]


async def test_deadline_in_past_rejected(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post(
        "/tasks", json=task_payload([seeded["teacher1@example.com"]],
                                    deadline=iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    )
    assert resp.status_code == 400

    resp = await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]], priority="MEGA"))
    assert resp.status_code == 400

    resp = await a.post("/tasks", json=task_payload(["00000000-0000-0000-0000-000000000001"]))
    assert resp.status_code == 400

    # дубли исполнителей схлопываются
    tid = seeded["teacher1@example.com"]
    resp = await a.post("/tasks", json=task_payload([tid, tid]))
    assert resp.status_code == 201
    assert len(resp.json()["assignees"]) == 1


# ------------------------------------------------------------------- видимость
async def test_teacher_sees_only_own_tasks(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    t2 = seeded["teacher2@example.com"]

    task1 = (await a.post("/tasks", json=task_payload([t1], title="Задача первого учителя"))).json()
    task2 = (await a.post("/tasks", json=task_payload([t2], title="Задача второго учителя"))).json()

    first = await auth(client, "teacher1@example.com")
    items = (await first.get("/tasks")).json()
    ids = {t["id"] for t in items["items"]}
    assert task1["id"] in ids
    assert task2["id"] not in ids
    assert items["total"] == 1

    # чужая задача — 403
    assert (await first.get(f"/tasks/{task2['id']}")).status_code == 403
    assert (await first.patch(f"/tasks/{task2['id']}", json={"title": "Чужая задача изменена"})).status_code == 403
    assert (await first.delete(f"/tasks/{task2['id']}")).status_code == 403
    assert (await first.post(f"/tasks/{task2['id']}/my-mark")).status_code == 403

    # админ видит обе
    all_items = (await a.get("/tasks")).json()
    assert all_items["total"] == 2
    assert (await a.get(f"/tasks/{task2['id']}")).status_code == 200


async def test_teacher_cannot_filter_by_others(client, seeded):
    t = await auth(client, "teacher1@example.com")
    resp = await t.get("/tasks", params={"assignee_id": seeded["teacher2@example.com"]})
    assert resp.status_code == 403
    resp = await t.get("/tasks", params={"assignee_id": seeded["teacher1@example.com"]})
    assert resp.status_code == 200


async def test_teacher_cannot_reassign_or_change_deadline(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()

    t = await auth(client, "teacher1@example.com")
    resp = await t.patch(f"/tasks/{task['id']}", json={"assignee_ids": [seeded["teacher2@example.com"]]})
    assert resp.status_code == 403
    resp = await t.patch(f"/tasks/{task['id']}", json={"deadline": iso(datetime.now(timezone.utc) + timedelta(days=30))})
    assert resp.status_code == 403
    # но описание и статус — можно
    resp = await t.patch(f"/tasks/{task['id']}", json={"description": "Уточнил объём работы"})
    assert resp.status_code == 200 and resp.json()["description"] == "Уточнил объём работы"


# ------------------------------------------------- отметки «выполнено» (v1.2)
async def test_marks_flow_partial_then_all_done(client, seeded):
    a = await auth(client, "admin@example.com")
    t1, t2 = seeded["teacher1@example.com"], seeded["teacher2@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1, t2], title="Общая задача"))).json()
    tid = task["id"]

    first = await auth(client, "teacher1@example.com")
    second = await auth(client, "teacher2@example.com")

    # одна отметка — задача «в работе», не закрыта
    resp = await first.post(f"/tasks/{tid}/my-mark")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "IN_PROGRESS"
    assert body["marks_done"] == 1
    assert body["my_done"] is True

    # повторная отметка — 409
    assert (await first.post(f"/tasks/{tid}/my-mark")).status_code == 409

    # постановщик получил уведомление об отметке
    notes = (await a.get("/notifications")).json()["items"]
    assert any(n["type"] == "TASK_MARK" and n["entity_id"] == tid for n in notes)

    # вторая отметка — задача автоматически закрывается
    resp = await second.post(f"/tasks/{tid}/my-mark")
    body = resp.json()
    assert body["status"] == "DONE" and body["completed_at"] is not None
    assert body["marks_done"] == 2

    notes = (await a.get("/notifications")).json()["items"]
    assert any(n["type"] == "TASK_STATUS_CHANGED" and "все исполнители" in n["title"].lower() for n in notes)

    # история содержит авто-переход
    detail = (await a.get(f"/tasks/{tid}")).json()
    # EVENT-строки (создание/назначение) не входят в последовательность
    hist = [h["to_status"] for h in detail["history"] if h["to_status"] != "EVENT"]
    assert hist == ["NEW", "IN_PROGRESS", "DONE"]

    # снятие отметки возвращает задачу в работу
    resp = await second.delete(f"/tasks/{tid}/my-mark")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "IN_PROGRESS" and body["completed_at"] is None and body["marks_done"] == 1

    # снять несуществующую отметку — 409
    assert (await second.delete(f"/tasks/{tid}/my-mark")).status_code == 409


async def test_mark_only_for_assignees(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()
    t2 = await auth(client, "teacher2@example.com")
    assert (await t2.post(f"/tasks/{task['id']}/my-mark")).status_code == 403
    assert (await t2.delete(f"/tasks/{task['id']}/my-mark")).status_code == 403
    # админ не исполнитель — тоже не отмечается
    assert (await a.post(f"/tasks/{task['id']}/my-mark")).status_code == 403


# ------------------------------------------------------------------ вложения
async def test_task_files_upload_download_delete(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()
    tid = task["id"]

    t = await auth(client, "teacher1@example.com")
    resp = await t.post(f"/tasks/{tid}/files", files=upload("ведомость.pdf", pdf_bytes("Ведомость"), "application/pdf"))
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    assert detail["files_count"] == 1
    file_id = detail["files"][0]["id"]
    assert detail["files"][0]["original_name"] == "ведомость.pdf"
    assert detail["files"][0]["uploaded_by"]["email"] == "teacher1@example.com"

    # посторонний не видит задачу и не качает файл
    t2 = await auth(client, "teacher2@example.com")
    assert (await t2.get(f"/tasks/{tid}/files/{file_id}/download")).status_code == 403

    # скачивание участником
    resp = await t.get(f"/tasks/{tid}/files/{file_id}/download")
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF")

    # недопустимый тип файла
    resp = await t.post(f"/tasks/{tid}/files", files=upload("virus.exe", b"MZ", "application/x-msdownload"))
    assert resp.status_code == 400

    # удалить может загрузивший или администрация
    assert (await a.delete(f"/tasks/{tid}/files/{file_id}")).status_code == 200
    assert (await a.get(f"/tasks/{tid}")).json()["files_count"] == 0


# --------------------------------------------------------------------- статусы
async def test_status_state_machine(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()
    tid = task["id"]

    resp = await a.post(f"/tasks/{tid}/status", json={"status": "DONE"})
    assert resp.status_code == 200 and resp.json()["status"] == "DONE"
    assert resp.json()["completed_at"] is not None

    # DONE -> NEW запрещён
    assert (await a.post(f"/tasks/{tid}/status", json={"status": "NEW"})).status_code == 409
    # DONE -> IN_PROGRESS разрешён (откат)
    assert (await a.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"})).status_code == 200
    # IN_PROGRESS -> NEW разрешён
    assert (await a.post(f"/tasks/{tid}/status", json={"status": "NEW"})).status_code == 200
    # тот же статус — no-op 200
    assert (await a.post(f"/tasks/{tid}/status", json={"status": "NEW"})).status_code == 200

    detail = (await a.get(f"/tasks/{tid}")).json()
    hist = [h["to_status"] for h in detail["history"] if h["to_status"] != "EVENT"]
    assert hist == ["NEW", "DONE", "IN_PROGRESS", "NEW"]


async def test_overdue_is_computed_not_stored(client, seeded, db_session):
    """Ключевая проверка: OVERDUE появляется сам, без cron'а и без записи в БД."""
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]], title="Просроченная задача"))).json()
    tid = task["id"]

    # «время прошло»: сдвигаем дедлайн в прошлое напрямую в БД
    from sqlalchemy import update

    from app.models.task import Task

    await db_session.execute(
        update(Task).where(Task.id == tid).values(deadline=datetime.now(timezone.utc) - timedelta(hours=5))
    )
    await db_session.commit()

    fresh = (await a.get(f"/tasks/{tid}")).json()
    assert fresh["status"] == "NEW"                 # хранимый статус не изменился
    assert fresh["effective_status"] == "OVERDUE"   # вычислено на лету
    assert fresh["is_overdue"] is True
    assert fresh["days_left"] < 0

    filtered = (await a.get("/tasks", params={"status": "OVERDUE"})).json()
    assert filtered["total"] == 1
    assert filtered["status_counts"]["OVERDUE"] == 1
    assert filtered["status_counts"]["NEW"] == 0    # просроченная не учитывается как «новая»

    t = await auth(client, "teacher1@example.com")
    mine = (await t.get("/tasks", params={"status": "OVERDUE"})).json()
    assert mine["total"] == 1

    done = await t.post(f"/tasks/{tid}/status", json={"status": "DONE"})
    assert done.status_code == 200
    assert done.json()["effective_status"] == "DONE"
    assert done.json()["is_overdue"] is False
    counts = (await t.get("/tasks/status-counts")).json()
    assert counts["OVERDUE"] == 0 and counts["DONE"] == 1


async def test_overdue_report_for_admin_only(client, seeded):
    a = await auth(client, "admin@example.com")
    t = await auth(client, "teacher1@example.com")

    report = (await a.get("/tasks/overdue-report")).json()
    assert report["items"] == []
    assert (await t.get("/tasks/overdue-report")).status_code == 403


async def test_overdue_report_groups_assignees(client, seeded, db_session):
    a = await auth(client, "admin@example.com")
    t1, t2 = seeded["teacher1@example.com"], seeded["teacher2@example.com"]
    task = (await a.post("/tasks", json=task_payload([t1, t2], title="Просрочка на двоих"))).json()

    from sqlalchemy import update

    from app.models.task import Task

    await db_session.execute(
        update(Task).where(Task.id == task["id"]).values(deadline=datetime.now(timezone.utc) - timedelta(days=2))
    )
    await db_session.commit()

    report = (await a.get("/tasks/overdue-report")).json()
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert sorted(item["assignees"]) == ["Учитель Второй", "Учитель Первый"]
    assert item["overdue_by_days"] >= 1


# ----------------------------------------------------------------- комментарии
async def test_comments_and_participant_rules(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()
    tid = task["id"]

    t1 = await auth(client, "teacher1@example.com")
    resp = await t1.post(f"/tasks/{tid}/comments", json={"body": "Взял в работу, сдам к пятнице"})
    assert resp.status_code == 201
    assert resp.json()["author"]["email"] == "teacher1@example.com"

    t2 = await auth(client, "teacher2@example.com")
    assert (await t2.post(f"/tasks/{tid}/comments", json={"body": "Посторонний комментарий"})).status_code == 403

    detail = (await a.get(f"/tasks/{tid}")).json()
    assert len(detail["comments"]) == 1

    # постановщик получил уведомление о комментарии
    notes = (await a.get("/notifications")).json()["items"]
    assert any(n["type"] == "TASK_COMMENT" for n in notes)


# --------------------------------------------------------------- фильтрация/сортировка
async def test_task_filters_and_sorting(client, seeded):
    a = await auth(client, "admin@example.com")
    t1, t2 = seeded["teacher1@example.com"], seeded["teacher2@example.com"]

    await a.post("/tasks", json=task_payload([t1], title="Альфа", priority="URGENT",
                                             deadline=iso(datetime.now(timezone.utc) + timedelta(days=1))))
    await a.post("/tasks", json=task_payload([t2], title="Бета", priority="LOW",
                                             deadline=iso(datetime.now(timezone.utc) + timedelta(days=9))))
    third = (await a.post("/tasks", json=task_payload([t1, t2], title="Гамма", priority="HIGH",
                                                      deadline=iso(datetime.now(timezone.utc) + timedelta(days=5))))).json()
    await a.post(f"/tasks/{third['id']}/status", json={"status": "IN_PROGRESS"})

    by_assignee = (await a.get("/tasks", params={"assignee_id": t1})).json()
    assert by_assignee["total"] == 2
    by_assignee2 = (await a.get("/tasks", params={"assignee_id": t2})).json()
    assert by_assignee2["total"] == 2      # «Гамма» — общая

    by_priority = (await a.get("/tasks", params={"priority": "LOW"})).json()
    assert [t["title"] for t in by_priority["items"]] == ["Бета"]

    by_status = (await a.get("/tasks", params={"status": "IN_PROGRESS"})).json()
    assert [t["title"] for t in by_status["items"]] == ["Гамма"]

    search = (await a.get("/tasks", params={"search": "гам"})).json()
    assert search["total"] == 1

    due = (await a.get("/tasks", params={"due_before": iso(datetime.now(timezone.utc) + timedelta(days=2))})).json()
    assert [t["title"] for t in due["items"]] == ["Альфа"]

    by_title_desc = (await a.get("/tasks", params={"sort": "title", "order": "desc"})).json()
    assert [t["title"] for t in by_title_desc["items"]] == ["Гамма", "Бета", "Альфа"]

    counts = (await a.get("/tasks/status-counts")).json()
    assert counts == {"all": 3, "ACTIVE": 3, "NEW": 2, "IN_PROGRESS": 1, "DONE": 0, "OVERDUE": 0}

    assert (await a.get("/tasks", params={"status": "NOPE"})).status_code == 422


async def test_admin_can_edit_any_task_and_delete(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))).json()

    resp = await a.patch(f"/tasks/{task['id']}", json={"title": "Переименовано администрацией", "priority": "URGENT"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Переименовано администрацией"

    # переназначение: заменить состав исполнителей
    resp = await a.patch(f"/tasks/{task['id']}", json={"assignee_ids": [seeded["teacher2@example.com"]]})
    assert resp.status_code == 200 and emails_of(resp.json()) == ["teacher2@example.com"]

    # добавление второго исполнителя без потери первого
    resp = await a.patch(f"/tasks/{task['id']}", json={
        "assignee_ids": [seeded["teacher2@example.com"], seeded["teacher1@example.com"]]})
    assert resp.status_code == 200
    assert len(resp.json()["assignees"]) == 2

    assert (await a.delete(f"/tasks/{task['id']}")).status_code == 204
    assert (await a.get(f"/tasks/{task['id']}")).status_code == 404


async def test_dashboard_scope_differs_by_role(client, seeded):
    a = await auth(client, "admin@example.com")
    await a.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))

    admin_dash = (await a.get("/dashboard")).json()
    assert admin_dash["summary"]["scope"] == "global"
    assert admin_dash["summary"]["users"]["total"] == 4
    assert admin_dash["summary"]["tasks"]["total"] == 1
    assert admin_dash["current_plan"] is None       # планов ещё нет

    t = await auth(client, "teacher1@example.com")
    teacher_dash = (await t.get("/dashboard")).json()
    assert teacher_dash["summary"]["scope"] == "personal"
    assert teacher_dash["summary"]["users"] is None      # справочник пользователей учителю недоступен
    assert teacher_dash["summary"]["tasks"]["total"] == 1
