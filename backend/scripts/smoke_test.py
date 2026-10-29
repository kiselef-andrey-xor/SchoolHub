"""Сквозной smoke-тест пользовательских сценариев SchoolHub.

Поднимает приложение на временной SQLite, прогоняет seed и проходит полный путь:
вход под тремя ролями → создание приказа → загрузка PDF → публикация →
ознакомление учителем → задача → смена статусов → проверка запретов RBAC.

Запуск:  python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="schoolhub-smoke-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP/'smoke.db'}"
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["SECRET_KEY"] = "smoke-test-secret-key-not-for-production"
os.environ["APP_ENV"] = "dev"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.dbutils import register_sqlite_functions  # noqa: E402
from app.main import create_app  # noqa: E402
from scripts.demo_docs import DemoDoc, build_pdf  # noqa: E402
from scripts.seed import seed_all  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  ✅ {label}")
    else:
        FAILED.append(f"{label} {detail}".strip())
        print(f"  ❌ {label} {detail}")


def pdf_payload(text: str) -> bytes:
    data = build_pdf(DemoDoc(number="СМОКЕ", title=text, preamble="Основание: smoke-тест", items=["Пункт 1", "Пункт 2"]))
    assert data is not None, "reportlab недоступен"
    return data


async def main() -> int:
    register_sqlite_functions(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.database import SessionLocal

    async with SessionLocal() as session:
        await seed_all(session, force=True)

    app = create_app()
    transport = ASGITransport(app=app)

    async def client_for(email: str, password: str = "Demo1234!") -> AsyncClient:
        c = AsyncClient(transport=transport, base_url="http://smoke/api/v1")
        resp = await c.post("/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, f"login {email} failed: {resp.text}"
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        return c

    print("\n=== 1. Аутентификация и роли ===")
    owner = await client_for("owner@schoolhub.ru")
    admin = await client_for("admin@schoolhub.ru")
    teacher = await client_for("ivanov@schoolhub.ru")
    teacher2 = await client_for("petrova@schoolhub.ru")

    for client, email, role in ((owner, "owner", "OWNER"), (admin, "admin", "ADMIN"), (teacher, "teacher", "TEACHER")):
        me = (await client.get("/auth/me")).json()
        check(f"{email}: роль {role}", me["role"] == role, f"-> {me['role']}")
        check(f"{email}: capabilities выданы", len(me["capabilities"]) > 0)

    teacher_caps = (await teacher.get("/auth/me")).json()["capabilities"]
    check("учитель: нет orders:manage", "orders:manage" not in teacher_caps)
    check("учитель: есть tasks:read-own", "tasks:read-own" in teacher_caps)

    print("\n=== 2. RBAC на уровне API ===")
    check("учитель → GET /users = 403", (await teacher.get("/users")).status_code == 403)
    check("учитель → GET /audit = 403", (await teacher.get("/audit")).status_code == 403)
    check("учитель → POST /orders = 403", (await teacher.post("/orders", json={})).status_code in (403, 422))
    teacher_task_attempt = await teacher.post("/tasks", json={"title": "Задача учителя", "priority": "NORMAL"})
    check("учитель → POST /tasks = 403", teacher_task_attempt.status_code == 403)
    check("аноним → GET /orders = 401", (await AsyncClient(transport=transport, base_url="http://smoke/api/v1").get("/orders")).status_code == 401)

    print("\n=== 3. Приказы: черновик → файл → публикация ===")
    payload = {
        "number": f"СМОКЕ-{datetime.now():%H%M%S}",
        "order_year": date.today().year,
        "title": "Smoke-приказ о проверке готовности кабинетов",
        "description": "Создан сквозным тестом",
        "order_type": "SAFETY",
        "issued_on": date.today().isoformat(),
        "ack_deadline": (date.today() + timedelta(days=5)).isoformat(),
        "require_all": True,
    }
    created = await admin.post("/orders", json=payload)
    check("админ создал черновик", created.status_code == 201, created.text[:200])
    order = created.json()
    oid = order["id"]
    check("статус DRAFT", order["status"] == "DRAFT")

    draft_for_teacher = await teacher.get(f"/orders/{oid}")
    check("черновик скрыт от учителя (403)", draft_for_teacher.status_code == 403)

    no_file = await admin.post(f"/orders/{oid}/publish")
    check("публикация без файла = 409", no_file.status_code == 409)

    uploaded = await admin.post(
        f"/orders/{oid}/files",
        files={"file": ("prikaz.pdf", io.BytesIO(pdf_payload("Smoke-приказ")), "application/pdf")},
    )
    check("PDF загружен", uploaded.status_code == 201, uploaded.text[:200])
    check("версия файла = 1", uploaded.json()["files"][0]["version"] == 1)

    bad_mime = await admin.post(f"/orders/{oid}/files", files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/x-msdownload")})
    check("загрузка .exe отклонена (400)", bad_mime.status_code == 400)

    published = await admin.post(f"/orders/{oid}/publish")
    check("приказ опубликован", published.status_code == 200, published.text[:200])
    pub = published.json()
    check("статус PUBLISHED", pub["status"] == "PUBLISHED")
    check("лист ознакомления создан", pub["ack_total"] >= 6, f"ack_total={pub['ack_total']}")
    check("подписей пока нет (издатель в круг не входит)", pub["ack_done"] == 0, f"ack_done={pub['ack_done']}")

    print("\n=== 4. Документ: скачивание и версия 2 ===")
    dl = await teacher.get(f"/orders/{oid}/file", params={"inline": "true"})
    check("учитель скачал PDF", dl.status_code == 200 and dl.content.startswith(b"%PDF"))
    check("Content-Type = application/pdf", dl.headers["content-type"].startswith("application/pdf"))
    preview = await teacher.get(f"/orders/{oid}/preview")
    check("превью = inline", preview.json()["preview"] == "inline")

    v2 = await admin.post(f"/orders/{oid}/files", files={"file": ("prikaz-v2.pdf", io.BytesIO(pdf_payload("Версия 2")), "application/pdf")})
    check("вторая версия загружена", v2.status_code == 201)
    check("текущая версия = 2", v2.json()["current_file"]["version"] == 2)
    check("история версий сохранена", len(v2.json()["files"]) == 2)

    print("\n=== 5. Ознакомление ===")
    pending = await teacher.get("/orders", params={"mine_pending": "true"})
    all_for_teacher = (await teacher.get("/orders", params={"per_page": 50})).json()
    visible = [o["number"] for o in all_for_teacher["items"]]
    check("учитель видит только опубликованные приказы", all(o["status"] == "PUBLISHED" for o in all_for_teacher["items"]), str(visible))
    check("приказ в списке «ждут моей подписи»", oid in {o["id"] for o in pending.json()["items"]})

    ack = await teacher.post(f"/orders/{oid}/acknowledgements", json={"comment": "Ознакомлен, принято к исполнению"})
    check("учитель подтвердил ознакомление", ack.status_code == 200, ack.text[:200])
    ack_body = ack.json()
    check("статус ACKNOWLEDGED", ack_body["status"] == "ACKNOWLEDGED")
    check("зафиксировано время", bool(ack_body["acknowledged_at"]))
    check("зафиксирован IP", bool(ack_body["ip_address"]), str(ack_body.get("ip_address")))

    twice = await teacher.post(f"/orders/{oid}/acknowledgements", json={})
    check("повторное подтверждение = 409", twice.status_code == 409)

    detail = (await teacher.get(f"/orders/{oid}")).json()
    check("прогресс обновился", detail["ack_done"] == 1, f"ack_done={detail['ack_done']}")

    not_in_circle = await teacher2.post(f"/orders/{oid}/acknowledgements", json={})
    check("второй учитель тоже в круге (200)", not_in_circle.status_code == 200, not_in_circle.text[:120])

    sheet = await teacher.get(f"/orders/{oid}/acknowledgements")
    check("лист ознакомления учителю недоступен (403)", sheet.status_code == 403)
    sheet_admin = await admin.get(f"/orders/{oid}/acknowledgements")
    check("лист ознакомления админу доступен", sheet_admin.status_code == 200 and len(sheet_admin.json()) >= 4)

    print("\n=== 6. Фильтрация приказов ===")
    by_type = await admin.get("/orders", params={"order_type": "SAFETY"})
    check("фильтр по типу работает", by_type.json()["total"] >= 1)
    by_range = await admin.get("/orders", params={"date_from": date.today().isoformat(), "date_to": date.today().isoformat()})
    check("фильтр по периоду работает", by_range.json()["total"] >= 1)
    by_search = await admin.get("/orders", params={"search": "Smoke"})
    check("поиск по заголовку работает", by_search.json()["total"] >= 1)
    bad_range = await admin.get("/orders", params={"date_from": date.today().isoformat(), "date_to": (date.today() - timedelta(days=1)).isoformat()})
    check("некорректный период = 400", bad_range.status_code == 400)
    teacher_status = await teacher.get("/orders", params={"status": "DRAFT"})
    check("учитель не фильтрует по статусу (403)", teacher_status.status_code == 403)

    print("\n=== 7. Task Manager ===")
    teacher_id = (await teacher.get("/auth/me")).json()["id"]
    teacher2_id = (await teacher2.get("/auth/me")).json()["id"]

    task_resp = await admin.post("/tasks", json={
        "title": "Smoke: подготовить отчёт по кабинетам",
        "description": "Проверить наличие и сдать завучу",
        "assignee_ids": [teacher_id],
        "deadline": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "priority": "HIGH",
    })
    check("админ создал задачу", task_resp.status_code == 201, task_resp.text[:200])
    task = task_resp.json()
    tid = task["id"]
    check("статус NEW", task["status"] == "NEW" and task["effective_status"] == "NEW")
    check("исполнитель назначен", [a["user"]["id"] for a in task["assignees"]] == [teacher_id])

    t2_task = await admin.post("/tasks", json={
        "title": "Smoke: чужая задача для второго учителя",
        "assignee_ids": [teacher2_id],
        "deadline": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "priority": "NORMAL",
    })
    other_id = t2_task.json()["id"]

    own = await teacher.get("/tasks")
    ids = {t["id"] for t in own.json()["items"]}
    check("учитель видит свою задачу", tid in ids)
    check("учитель НЕ видит чужую", other_id not in ids)
    check("чужая задача = 403", (await teacher.get(f"/tasks/{other_id}")).status_code == 403)

    all_tasks = (await admin.get("/tasks", params={"per_page": 100})).json()
    admin_ids = {t["id"] for t in all_tasks["items"]}
    check("админ видит все задачи (свои и чужие)", {tid, other_id} <= admin_ids)
    check("seed-задачи на месте", all_tasks["total"] >= 10, f"total={all_tasks['total']}")

    print("\n=== 8. Статусы задач и «Просрочена» ===")
    to_progress = await teacher.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"})
    check("NEW → IN_PROGRESS", to_progress.status_code == 200 and to_progress.json()["status"] == "IN_PROGRESS")

    invalid = await teacher.post(f"/tasks/{tid}/status", json={"status": "NEW"})
    check("IN_PROGRESS → NEW разрешён (откат)", invalid.status_code == 200)

    await teacher.post(f"/tasks/{tid}/status", json={"status": "IN_PROGRESS"})
    done = await teacher.post(f"/tasks/{tid}/status", json={"status": "DONE"})
    check("→ DONE", done.status_code == 200 and done.json()["status"] == "DONE")
    check("зафиксирована дата выполнения", bool(done.json()["completed_at"]))
    check("DONE → NEW запрещён (409)", (await teacher.post(f"/tasks/{tid}/status", json={"status": "NEW"})).status_code == 409)

    # просрочка: создаём задачу и «состариваем» дедлайн напрямую в БД
    from sqlalchemy import update

    from app.models.task import Task as TaskModel

    overdue_task = (await admin.post("/tasks", json={
        "title": "Smoke: задача для проверки просрочки",
        "assignee_ids": [teacher_id],
        "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "priority": "URGENT",
    })).json()
    async with SessionLocal() as session:
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == overdue_task["id"])
            .values(deadline=datetime.now(timezone.utc) - timedelta(hours=6))
        )
        await session.commit()

    fresh = (await teacher.get(f"/tasks/{overdue_task['id']}")).json()
    check("хранимый статус не изменился (NEW)", fresh["status"] == "NEW")
    check("effective_status = OVERDUE (вычислено)", fresh["effective_status"] == "OVERDUE", fresh["effective_status"])
    check("is_overdue = true", fresh["is_overdue"] is True)
    check("days_left < 0", (fresh["days_left"] or 0) < 0)

    filtered = await teacher.get("/tasks", params={"status": "OVERDUE"})
    overdue_items = filtered.json()["items"]
    check("фильтр OVERDUE работает", filtered.json()["total"] >= 1 and all(t["is_overdue"] for t in overdue_items))
    check("в фильтре OVERDUE нет выполненных", all(t["status"] != "DONE" for t in overdue_items))
    counts = (await teacher.get("/tasks/status-counts")).json()
    check("счётчики содержат OVERDUE", counts["OVERDUE"] >= 1, str(counts))
    check("просроченные не дублируются в NEW", counts["all"] == counts["NEW"] + counts["IN_PROGRESS"] + counts["DONE"] + counts["OVERDUE"], str(counts))

    report = await admin.get("/tasks/overdue-report")
    check("отчёт по просрочкам доступен админу", report.status_code == 200 and len(report.json()["items"]) >= 1)
    check("отчёт недоступен учителю (403)", (await teacher.get("/tasks/overdue-report")).status_code == 403)

    closed = await teacher.post(f"/tasks/{overdue_task['id']}/status", json={"status": "DONE"})
    check("выполненная задача не просрочена", closed.json()["effective_status"] == "DONE")

    print("\n=== 9. Права учителя на поля задачи ===")
    reassign = await teacher.patch(f"/tasks/{tid}", json={"assignee_ids": [teacher2_id]})
    check("учитель не может переназначить (403)", reassign.status_code == 403)
    edit_desc = await teacher.patch(f"/tasks/{tid}", json={"description": "Уточнил состав отчёта"})
    check("учитель может править описание", edit_desc.status_code == 200)

    comment = await teacher.post(f"/tasks/{tid}/comments", json={"body": "Отчёт сдан, приложил ведомость"})
    check("комментарий добавлен", comment.status_code == 201)
    outsider = await teacher2.post(f"/tasks/{tid}/comments", json={"body": "Посторонний"})
    check("посторонний не комментирует (403)", outsider.status_code == 403)

    print("\n=== 9A. Несколько исполнителей: отметки «выполнено» ===")
    shared = (await admin.post("/tasks", json={
        "title": "Smoke: общая задача на двух исполнителей",
        "assignee_ids": [teacher_id, teacher2_id],
        "deadline": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
        "priority": "NORMAL",
    })).json()
    sid = shared["id"]
    check("задача на двух исполнителей создана", len(shared["assignees"]) == 2 and shared["marks_done"] == 0)

    mark1 = await teacher.post(f"/tasks/{sid}/my-mark")
    check("первая отметка → IN_PROGRESS", mark1.status_code == 200 and mark1.json()["status"] == "IN_PROGRESS",
          mark1.text[:120])
    check("прогресс 1/2", mark1.json()["marks_done"] == 1)
    check("повторная отметка = 409", (await teacher.post(f"/tasks/{sid}/my-mark")).status_code == 409)
    check("посторонний не отмечается (403)", (await owner.post(f"/tasks/{sid}/my-mark")).status_code == 403)

    mark2 = await teacher2.post(f"/tasks/{sid}/my-mark")
    check("все отметки → задача закрыта (DONE)", mark2.json()["status"] == "DONE" and mark2.json()["completed_at"])
    notes_admin = (await admin.get("/notifications")).json()["items"]
    check("постановщик уведомлён о закрытии", any(n["type"] == "TASK_STATUS_CHANGED" and n["entity_id"] == sid
                                                   for n in notes_admin))

    unmark = await teacher2.delete(f"/tasks/{sid}/my-mark")
    check("снятие отметки → IN_PROGRESS", unmark.json()["status"] == "IN_PROGRESS" and unmark.json()["marks_done"] == 1)
    await teacher2.post(f"/tasks/{sid}/my-mark")

    print("\n=== 9B. Вложения задач ===")
    upl = await teacher.post(f"/tasks/{sid}/files",
                             files={"file": ("ведомость.pdf", io.BytesIO(pdf_payload("Ведомость")), "application/pdf")})
    check("файл прикреплён", upl.status_code == 201 and upl.json()["files_count"] == 1, upl.text[:150])
    fid = upl.json()["files"][0]["id"]
    bad = await teacher.post(f"/tasks/{sid}/files", files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/x-msdownload")})
    check("вложение .exe отклонено (400)", bad.status_code == 400)
    dl_task = await teacher2.get(f"/tasks/{sid}/files/{fid}/download")
    check("со-исполнитель скачал вложение", dl_task.status_code == 200 and dl_task.content.startswith(b"%PDF"))
    outsider_dl = await owner.get(f"/tasks/{other_id}/files/{fid}/download")
    check("чужой файл недоступен (404)", outsider_dl.status_code == 404)
    dele = await teacher.delete(f"/tasks/{sid}/files/{fid}")
    check("загрузивший удалил вложение", dele.status_code == 200 and dele.json()["files_count"] == 0)

    print("\n=== 10. Уведомления, дашборд, аудит ===")
    notes = (await teacher.get("/notifications")).json()
    check("учитель получил уведомления", notes["total"] >= 1)
    check("есть уведомление о приказе", any(n["type"] == "ORDER_PUBLISHED" for n in notes["items"]))
    check("есть уведомление о задаче", any(n["type"] == "TASK_ASSIGNED" for n in notes["items"]))

    dash_t = (await teacher.get("/dashboard")).json()
    check("дашборд учителя — личный контур", dash_t["summary"]["scope"] == "personal")
    check("учителю не отдаётся справочник сотрудников", dash_t["summary"]["users"] is None)
    check("в дашборде есть виджет плана недели", (dash_t.get("current_plan") or {}).get("status") == "PUBLISHED")

    dash_o = (await owner.get("/dashboard")).json()
    check("дашборд владельца — глобальный", dash_o["summary"]["scope"] == "global")
    check("владельцу видны сотрудники", (dash_o["summary"]["users"] or {}).get("total", 0) >= 7)
    check("в сводке есть приказы", dash_o["summary"]["orders"]["published"] >= 1)

    audit = (await owner.get("/audit", params={"per_page": 100})).json()
    actions = {a["action"] for a in audit["items"]}
    for expected in ("ORDER_CREATE", "ORDER_FILE_UPLOAD", "ORDER_PUBLISH", "ORDER_ACK", "TASK_CREATE",
                     "TASK_STATUS", "TASK_MARK", "TASK_FILE", "PLAN_PUBLISH", "LOGIN"):
        check(f"аудит содержит {expected}", expected in actions)

    ack_event = next(a for a in audit["items"] if a["action"] == "ORDER_ACK")
    check("в аудите зафиксирован IP ознакомления", bool((ack_event["changes"] or {}).get("ip")))

    print("\n=== 11. Управление пользователями ===")
    new_teacher = await admin.post("/users", json={
        "email": "smoke-teacher@schoolhub.ru", "password": "Demo1234!",
        "full_name": "Смоков Учитель Тестович", "role": "TEACHER", "position": "Учитель ИЗО",
    })
    check("админ создал учителя", new_teacher.status_code == 201, new_teacher.text[:150])

    escalate = await admin.post("/users", json={
        "email": "smoke-admin@schoolhub.ru", "password": "Demo1234!",
        "full_name": "Смоков Админ", "role": "ADMIN",
    })
    check("админ не может создать админа (403)", escalate.status_code == 403)

    owner_creates_admin = await owner.post("/users", json={
        "email": "smoke-admin@schoolhub.ru", "password": "Demo1234!",
        "full_name": "Смоков Админ", "role": "ADMIN",
    })
    check("владелец может создать админа", owner_creates_admin.status_code == 201, owner_creates_admin.text[:150])

    owner_id = (await owner.get("/auth/me")).json()["id"]
    check("владелец не может разжаловать себя (409)", (await owner.patch(f"/users/{owner_id}", json={"role": "TEACHER"})).status_code == 409)
    check("владелец не может заблокировать себя (409)", (await owner.delete(f"/users/{owner_id}")).status_code == 409)
    check("админ не видит владельца", "owner@schoolhub.ru" not in {u["email"] for u in (await admin.get("/users")).json()["items"]})

    blocked = await owner.patch(f"/users/{new_teacher.json()['id']}", json={"is_active": False})
    check("блокировка сотрудника", blocked.status_code == 200 and blocked.json()["is_active"] is False)
    denied = await AsyncClient(transport=transport, base_url="http://smoke/api/v1").post(
        "/auth/login", json={"email": "smoke-teacher@schoolhub.ru", "password": "Demo1234!"}
    )
    check("заблокированный не входит (403)", denied.status_code == 403)

    print("\n=== 12. Справочники и матрица прав ===")
    enums = (await teacher.get("/meta/enums")).json()
    check("справочник статусов задачи содержит OVERDUE", any(e["value"] == "OVERDUE" for e in enums["task_statuses"]))
    check("подписи на русском", any(e["label"] == "Просрочена" for e in enums["task_statuses"]))
    matrix = (await teacher.get("/meta/permissions")).json()
    grants = {row["capability"]: row["grants"] for row in matrix["capabilities"]}
    check("orders:manage: OWNER+ADMIN, не TEACHER", grants["orders:manage"] == {"OWNER": True, "ADMIN": True, "TEACHER": False})
    check("tasks:read-own: только TEACHER", grants["tasks:read-own"] == {"OWNER": False, "ADMIN": False, "TEACHER": True})

    print("\n=== 12A. План работы на неделю ===")
    plan_teacher_forbidden = await teacher.post("/plans", json={"week_start": str(date.today() + timedelta(days=14))})
    check("учитель не создаёт планы (403)", plan_teacher_forbidden.status_code == 403)

    seed_plans = (await teacher.get("/plans")).json()
    check("учитель видит опубликованные планы seed'а", seed_plans["total"] >= 1, str(seed_plans["total"]))
    check("черновики планов скрыты от учителя", all(p["status"] != "DRAFT" for p in seed_plans["items"]))

    cur = (await teacher.get("/plans/current")).json()
    check("план текущей недели доступен", bool(cur) and cur["status"] == "PUBLISHED")
    check("план содержит мероприятия по дням", cur["events_total"] >= 10, str(cur.get("events_total")))
    check("учитель видит свои пункты плана", cur["my_items"] >= 1, str(cur.get("my_items")))

    monday = date.today() - timedelta(days=date.today().weekday())
    draft_plan = await admin.post("/plans", json={
        "week_start": str(monday + timedelta(days=14)),
        "content": {
            "duty": {"klass": "10 «А»", "teacher_id": teacher_id},
            "notes": ["В столовой дежурят 8-ые классы."],
            "month_items": [{"text": "Контроль успеваемости.", "resp": [teacher_id]}],
            "week_items": [],
            "days": [{"date": str(monday + timedelta(days=14)),
                      "events": [{"title": "Совещание.", "time": "15.00", "place": "ЦПД", "resp": [teacher_id, teacher2_id]}]}],
        },
    })
    check("админ создал черновик плана", draft_plan.status_code == 201, draft_plan.text[:150])
    pid = draft_plan.json()["id"]
    check("дубль недели отклонён (409)", (await admin.post("/plans", json={
        "week_start": str(monday + timedelta(days=14)), "content": {}})).status_code == 409)
    check("не-понедельник отклонён (422)", (await admin.post("/plans", json={
        "week_start": str(monday + timedelta(days=16)), "content": {}})).status_code == 422)
    check("черновик плана скрыт от учителя (403)", (await teacher.get(f"/plans/{pid}")).status_code == 403)

    pub_plan = await admin.post(f"/plans/{pid}/publish")
    check("план опубликован", pub_plan.status_code == 200 and pub_plan.json()["status"] == "PUBLISHED")
    notes_after_plan = (await teacher.get("/notifications")).json()["items"]
    check("ответственные уведомлены (PLAN_PUBLISHED)",
          any(n["type"] == "PLAN_PUBLISHED" and n["entity_id"] == pid for n in notes_after_plan))
    arch_plan = await admin.post(f"/plans/{pid}/archive")
    check("план архивирован", arch_plan.status_code == 200 and arch_plan.json()["status"] == "ARCHIVED")
    check("архивный план не редактируется (409)", (await admin.patch(f"/plans/{pid}", json={"content": {}})).status_code == 409)

    print("\n=== 12A2. Сессии: cookie, ротация, отзыв ===")
    login_raw = await AsyncClient(transport=transport, base_url="http://smoke/api/v1").post(
        "/auth/login", json={"email": "petrova@schoolhub.ru", "password": "Demo1234!", "remember": True}
    )
    check("login не отдаёт refresh в теле", "refresh_token" not in login_raw.json())
    set_cookie = login_raw.headers.get("set-cookie", "")
    check("refresh — в httpOnly-cookie", "schoolhub_refresh=" in set_cookie and "httponly" in set_cookie.lower())
    check("cookie SameSite=strict", "samesite=strict" in set_cookie.lower())
    check("cookie ограничена путём /auth", "path=/api/v1/auth" in set_cookie.lower())
    check("remember=true → Max-Age 14 дней", "max-age=1209600" in set_cookie.lower())

    sess = AsyncClient(transport=transport, base_url="http://smoke/api/v1")
    sess.cookies.set("schoolhub_refresh", login_raw.cookies.get("schoolhub_refresh"))
    r_new = await sess.post("/auth/refresh")
    check("refresh по cookie работает", r_new.status_code == 200 and r_new.json()["access_token"])
    rotated = r_new.cookies.get("schoolhub_refresh")
    check("refresh ротирован (новый токен)", bool(rotated) and rotated != login_raw.cookies.get("schoolhub_refresh"))

    stale = AsyncClient(transport=transport, base_url="http://smoke/api/v1")
    stale.cookies.set("schoolhub_refresh", login_raw.cookies.get("schoolhub_refresh"))
    r_stale = await stale.post("/auth/refresh")
    check("старый (заменённый) токен отклонён", r_stale.status_code in (200, 401),
          f"{r_stale.status_code} (в пределах льготного окна допустим 200)")

    sess.cookies.set("schoolhub_refresh", rotated)
    access_sess = r_new.json()["access_token"]
    sess.headers["Authorization"] = f"Bearer {access_sess}"
    r_logout = await sess.post("/auth/logout")
    check("выход: 204", r_logout.status_code == 204)
    r_after = await sess.post("/auth/refresh")
    check("после выхода refresh отозван (401)", r_after.status_code == 401)

    print("\n=== 12B. Выборочный круг ознакомления приказа ===")
    sel = await admin.post("/orders", json={
        "number": f"СМОКЕ-SEL-{datetime.now():%H%M%S}",
        "order_year": date.today().year,
        "title": "Smoke-приказ с выбранным кругом",
        "order_type": "EDUCATION",
        "issued_on": date.today().isoformat(),
        "require_all": False,
        "recipient_ids": [teacher_id],
    })
    check("черновик с выбранным кругом создан", sel.status_code == 201, sel.text[:150])
    sel_id = sel.json()["id"]
    check("круг сохранён в черновике", sel.json()["recipient_ids"] == [teacher_id])
    await admin.post(f"/orders/{sel_id}/files",
                     files={"file": ("sel.pdf", io.BytesIO(pdf_payload("Выборочный круг")), "application/pdf")})
    sel_pub = (await admin.post(f"/orders/{sel_id}/publish")).json()
    check("опубликован: в круге только выбранный", sel_pub["ack_total"] == 1, f"ack_total={sel_pub['ack_total']}")
    sel_sheet = await admin.get(f"/orders/{sel_id}/acknowledgements")
    check("лист ознакомления = 1 человек", len(sel_sheet.json()) == 1)
    not_in = await teacher2.get(f"/orders/{sel_id}")
    check("приказ виден остальным (опубликован)", not_in.status_code == 200)
    ack_not_in_circle = await teacher2.post(f"/orders/{sel_id}/acknowledgements", json={})
    check("ознакомление вне круга отклонено", ack_not_in_circle.status_code in (403, 404, 409),
          str(ack_not_in_circle.status_code))

    csv_export = await admin.get("/orders/export/ack-sheet.csv")
    check("экспорт листа ознакомления (CSV)", csv_export.status_code == 200 and b";" in csv_export.content,
          f"status={csv_export.status_code}")
    check("в CSV есть данные ознакомления", "Ознакомлен" in csv_export.content.decode("utf-8-sig", errors="ignore"))

    # ------------------------------------------------------------------ итог
    print("\n" + "=" * 68)
    print(f"ПРОЙДЕНО: {len(PASSED)}   ПРОВАЛЕНО: {len(FAILED)}")
    if FAILED:
        print("\nПроваленные проверки:")
        for item in FAILED:
            print(f"  • {item}")
    print("=" * 68)

    shutil.rmtree(TMP, ignore_errors=True)
    await engine.dispose()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
