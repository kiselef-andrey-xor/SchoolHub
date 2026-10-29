"""Сводка с переключением области, удаление уведомлений, разбор DOCX-плана."""
from __future__ import annotations

import io

import pytest

from tests.conftest import DEMO_PASSWORD, auth, new_client, upload
from tests.test_orders import create_published
from tests.test_tasks import task_payload

pytestmark = pytest.mark.asyncio


def _plan_docx_bytes() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("План работы")
    d.add_paragraph("12.10.2026г. – 17.10.2026г.")
    d.add_paragraph("Дежурный класс: 9Б, кл.рук. Иванов Иван")
    d.add_paragraph("В столовой дежурят 7-ые классы.")
    d.add_paragraph("В течение месяца: 1. Контроль адаптации. Отв.: Иванов Иван")
    d.add_paragraph("В течение недели: 1. Конкурс. Отв.: Иванов Иван")
    table = d.add_table(rows=3, cols=5)
    head = ["Дата", "Мероприятие", "Время", "Место проведения", "Ответственный"]
    for i, h in enumerate(head):
        table.rows[0].cells[i].text = h
    row1 = ["12.10.2026 (понедельник)", "1. Совещание.", "09.00", "Каб. 1", "Иванов Иван"]
    for i, v in enumerate(row1):
        table.rows[1].cells[i].text = v
    row2 = ["", "2. Урок.", "10.00", "Каб. 2", "Петров Пётр"]
    for i, v in enumerate(row2):
        table.rows[2].cells[i].text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


async def test_dashboard_scope_switch(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    school = await admin.get("/dashboard", params={"scope": "school"})
    assert school.status_code == 200
    assert school.json()["summary"]["scope"] == "global"
    assert school.json()["summary"]["users"] is not None

    mine = await admin.get("/dashboard", params={"scope": "mine"})
    assert mine.status_code == 200
    body = mine.json()
    assert body["summary"]["scope"] == "personal"
    assert body["summary"]["users"] is None

    # учителю сводка по школе недоступна
    resp = await teacher.get("/dashboard", params={"scope": "school"})
    assert resp.status_code == 403
    resp = await teacher.get("/dashboard", params={"scope": "mine"})
    assert resp.status_code == 200


async def test_delete_own_notification(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    await create_published(admin, number="777-к")   # публикация создаёт уведомления

    listing = await teacher.get("/notifications")
    items = listing.json()["items"]
    assert items, "ожидались уведомления о публикации"
    nid = items[0]["id"]

    # чужое уведомление удалить нельзя (404, не раскрывая существование)
    resp = await admin.delete(f"/notifications/{nid}")
    assert resp.status_code == 404

    resp = await teacher.delete(f"/notifications/{nid}")
    assert resp.status_code == 204
    after = await teacher.get("/notifications")
    assert all(i["id"] != nid for i in after.json()["items"])


async def test_plan_parse_document(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    resp = await admin.post(
        "/plans/parse-document",
        files=upload("plan.docx", _plan_docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["week_start"] == "2026-10-12"
    content = body["content"]
    assert content["duty"]["klass"] == "9Б"
    assert content["notes"] == ["В столовой дежурят 7-ые классы."]
    assert len(content["month_items"]) == 1
    assert len(content["week_items"]) == 1
    assert len(content["days"]) == 1
    day = content["days"][0]
    assert day["date"] == "2026-10-12"
    # сотрудников с такими именами в системе нет → имена сохранены в тексте мероприятий
    assert [e["title"] for e in day["events"]] == [
        "1. Совещание. Отв.: Иванов Иван",
        "2. Урок. Отв.: Петров Пётр",
    ]
    assert day["events"][0]["time"] == "09.00"
    assert day["events"][0]["place"] == "Каб. 1"
    assert any("Иванов" in w for w in body["warnings"])

    # учителю разбор недоступен
    resp = await teacher.post(
        "/plans/parse-document",
        files=upload("plan.docx", _plan_docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert resp.status_code == 403

    # не-docx отклоняется
    resp = await admin.post("/plans/parse-document", files=upload("plan.pdf", b"%PDF-1.4\n", "application/pdf"))
    assert resp.status_code == 400


async def test_clear_all_notifications(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")
    await create_published(admin, number="778-к")

    listing = await teacher.get("/notifications")
    assert listing.json()["total"] > 0

    # владелец не может почистить чужие уведомления учителя (у самого учителя — может)
    resp = await teacher.delete("/notifications")
    assert resp.status_code == 204
    listing = await teacher.get("/notifications")
    assert listing.json()["total"] == 0


async def test_tasks_scope_mine_for_staff(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    resp = await owner.post("/tasks", json=task_payload([seeded["teacher1@example.com"]]))
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # администрация: «только мои» не содержит чужую задачу, «по школе» — содержит
    mine = await admin.get("/tasks", params={"scope": "mine"})
    assert mine.status_code == 200
    assert all(t["id"] != task_id for t in mine.json()["items"])
    assert mine.json()["status_counts"]["all"] == 0

    school = await admin.get("/tasks", params={"scope": "school"})
    assert any(t["id"] == task_id for t in school.json()["items"])

    # учителю общешкольный список недоступен
    assert (await teacher.get("/tasks", params={"scope": "school"})).status_code == 403
    # у учителя свои задачи видны по умолчанию
    own = await teacher.get("/tasks")
    assert any(t["id"] == task_id for t in own.json()["items"])


async def test_notifications_removed_with_order(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    order = await create_published(admin, number="779-к")
    before = await teacher.get("/notifications")
    assert any(n["entity_id"] == order["id"] for n in before.json()["items"])

    assert (await admin.post(f"/orders/{order['id']}/archive")).status_code == 200
    assert (await owner.delete(f"/orders/{order['id']}")).status_code == 204

    after = await teacher.get("/notifications")
    assert all(n["entity_id"] != order["id"] for n in after.json()["items"])


async def test_delete_and_restore_user(client, seeded, db_session):

    from sqlalchemy import select

    from app.models.user import User
    from tests.test_totp_pep import sign

    owner, sig = await sign(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    tid = seeded["teacher2@example.com"]

    # удаляет только Владелец
    resp = await admin.delete(f"/users/{tid}")
    assert resp.status_code == 403
    # без ПЭП «код + слово» удаление блокируется
    resp = await owner.delete(f"/users/{tid}")
    assert resp.status_code == 409, resp.text
    # себя удалить нельзя
    resp = await owner.request("DELETE", f"/users/{seeded['owner@example.com']}", json=sig())
    assert resp.status_code == 409

    email_before = "teacher2@example.com"
    resp = await owner.request("DELETE", f"/users/{tid}", json=sig())
    assert resp.status_code == 204, resp.text

    # в обычном списке нет, у владельца с include_deleted — есть
    listing = (await admin.get("/users")).json()
    assert all(u["id"] != tid for u in listing["items"])
    listing = (await owner.get("/users", params={"include_deleted": "true"})).json()
    deleted = next(u for u in listing["items"] if u["id"] == tid)
    assert deleted["is_deleted"] is True

    # вход закрыт
    login = await new_client(client).post(
        "/auth/login", json={"email": email_before, "password": DEMO_PASSWORD}
    )
    assert login.status_code in (401, 403)

    # e-mail освобождён — можно завести нового сотрудника на тот же адрес
    resp = await owner.post(
        "/users",
        json={"email": email_before, "password": "Passw0rd!", "full_name": "Вторая Учительница", "role": "TEACHER"},
    )
    assert resp.status_code == 201, resp.text

    # восстановление: учётка снова в списке, но выключена
    resp = await owner.post(f"/users/{tid}/restore")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    row = (await db_session.execute(select(User).where(User.id == __import__("uuid").UUID(tid)))).scalar_one()
    assert row.is_deleted is False


async def test_password_must_change_flow(client, seeded):
    owner = await auth(client, "owner@example.com")
    tid = seeded["teacher1@example.com"]

    resp = await owner.patch(f"/users/{tid}", json={"password_must_change": True})
    assert resp.status_code == 200

    teacher = await auth(client, "teacher1@example.com")
    me = (await teacher.get("/auth/me")).json()
    assert me["password_must_change"] is True

    # админ не может ставить флаг
    admin = await auth(client, "admin@example.com")
    resp = await admin.patch(f"/users/{tid}", json={"password_must_change": False})
    assert resp.status_code == 403

    # после смены своего пароля флаг снимается
    resp = await teacher.post(
        "/auth/change-password", json={"current_password": DEMO_PASSWORD, "new_password": "NewPassw0rd!"}
    )
    assert resp.status_code == 204
    login2 = await new_client(client).post(
        "/auth/login", json={"email": "teacher1@example.com", "password": "NewPassw0rd!"}
    )
    assert login2.status_code == 200
    teacher_fresh = new_client(client)
    teacher_fresh.headers["Authorization"] = f"Bearer {login2.json()['access_token']}"
    me = (await teacher_fresh.get("/auth/me")).json()
    assert me["password_must_change"] is False


async def test_admin_password_reset_sets_must_change(client, seeded):
    owner = await auth(client, "owner@example.com")
    tid = seeded["teacher2@example.com"]
    resp = await owner.patch(f"/users/{tid}", json={"password": "ResetPass1!"})
    assert resp.status_code == 200
    login3 = await new_client(client).post(
        "/auth/login", json={"email": "teacher2@example.com", "password": "ResetPass1!"}
    )
    assert login3.status_code == 200
    t2 = new_client(client)
    t2.headers["Authorization"] = f"Bearer {login3.json()['access_token']}"
    me = (await t2.get("/auth/me")).json()
    assert me["password_must_change"] is True


async def test_last_login_updates_on_activity(client, seeded, db_session):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models.user import User

    teacher = await auth(client, "teacher1@example.com")
    tid = seeded["teacher1@example.com"]
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = (await db_session.execute(select(User).where(User.id == __import__("uuid").UUID(tid)))).scalar_one()
    row.last_login_at = old
    await db_session.commit()

    # любой авторизованный запрос освежает метку (порог 5 минут)
    resp = await teacher.get("/auth/me")
    assert resp.status_code == 200
    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.id == __import__("uuid").UUID(tid)))).scalar_one()
    value = row.last_login_at
    if value is not None and value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    assert value is not None and value > old.replace(tzinfo=None) + timedelta(days=1)


async def test_backup_endpoint_owner_only(client, seeded):
    admin = await auth(client, "admin@example.com")
    owner = await auth(client, "owner@example.com")
    assert (await admin.get("/system/backup")).status_code == 403
    from tests.conftest import export_ticket

    ticket = await export_ticket(owner, "owner@example.com", "db_backup")
    resp = await owner.get("/system/backup", params={"ticket": ticket})
    # тестовая среда — SQLite: ожидаем понятную 400 (дамп возможен только из PostgreSQL);
    # на NAS с PostgreSQL — 200, либо 503 с кодом BACKUP_UNAVAILABLE / BACKUP_VERSION_MISMATCH
    assert resp.status_code in (200, 400, 503)
    if resp.status_code == 503:
        assert resp.json()["code"] in {"BACKUP_UNAVAILABLE", "BACKUP_VERSION_MISMATCH", "BACKUP_FAILED", "BACKUP_TIMEOUT"}
