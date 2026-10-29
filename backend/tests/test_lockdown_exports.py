"""Локдаун и экранирование в ответах, которые собираются ВРУЧНУЮ (не через Pydantic).

Сериализаторы схем перехватывают только JSON-ответы. CSV-выгрузки, печатные формы
и текст журнала событий строятся из ORM-объектов напрямую, поэтому до правки
раскрывали персональные данные при включённом локдауне (review/REVIEW.md §P1-1),
а печатная форма записки — ещё и stored XSS (§P0-2).

Здесь проверяется сквозной HTTP-контракт:
  * `GET /notes/export.csv`, `GET /movement/export.csv` — метки вместо ФИО;
  * `GET /notes/{id}/print` — метки вместо ФИО + экранирование пользовательского ввода;
  * `GET /audit` — обезличенный `changes`, `summary` и `lines`;
  * Владелец во всех случаях видит исходные значения (иначе режим теряет смысл).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from tests.conftest import auth
from tests.test_service_notes import draft, note_payload

pytestmark = pytest.mark.asyncio


async def _set_lockdown(owner, enabled: bool, sig) -> None:
    resp = await owner.post("/system/lockdown", json={"enabled": enabled, **sig()})
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------- CSV и печатная форма
async def test_notes_csv_and_print_form_masked_under_lockdown(client, seeded):
    from tests.test_totp_pep import sign

    # записка с «опасной» темой: она же проверяет экранирование печатной формы
    payload = note_payload(subject="<script>alert('xss')</script> Ремонт & «кабинет»")
    teacher, _ = await draft(client)
    resp = await teacher.post("/notes", json=payload)
    assert resp.status_code == 201, resp.text
    evil = resp.json()

    owner, sig = await sign(client, "owner@example.com")
    await _set_lockdown(owner, True, sig)

    # --- CSV журнала записок: ФИО автора и исполнителя заменены метками
    csv_text = (await teacher.get("/notes/export.csv")).text
    assert "Учитель Первый" not in csv_text
    assert "Учитель Второй" not in csv_text
    assert "Сотрудник " in csv_text
    assert csv_text.startswith("\ufeff")                       # BOM для Excel сохранён

    # --- печатная форма: метки вместо ФИО и никакого исполняемого HTML
    html = (await teacher.get(f"/notes/{evil['id']}/print")).text
    assert "Учитель Первый" not in html
    assert "<script>alert('xss')</script>" not in html         # тег экранирован
    assert "&lt;script&gt;" in html                            # показан как текст
    assert "&amp;" in html                                     # «&» тоже экранируется
    printed = await teacher.get(f"/notes/{evil['id']}/print")
    assert printed.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert printed.headers["X-Content-Type-Options"] == "nosniff"

    # --- Владелец видит настоящие ФИО
    owner_csv = (await owner.get("/notes/export.csv")).text
    assert "Учитель Первый" in owner_csv

    await _set_lockdown(owner, False, sig)
    csv_text = (await teacher.get("/notes/export.csv")).text
    assert "Учитель Первый" in csv_text


async def test_movement_csv_masked_under_lockdown(client, seeded, db_session):
    from app.models.movement import MovementChild, MovementOrder
    from tests.test_totp_pep import sign

    order = MovementOrder(
        order_type="ENROLLMENT",
        student="Иванов Пётр Сергеевич",
        guardian="Иванова Мария Петровна",
        order_date=date(2026, 9, 1),
        order_number="123-ОД",
        signatory="Директоров Андрей Андреевич",
        birth_date=date(2015, 4, 2),
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(MovementChild(movement_id=order.id, student="Иванов Пётр Сергеевич",
                                 birth_date=date(2015, 4, 2), position=0))
    await db_session.commit()

    owner, sig = await sign(client, "owner@example.com")
    assert (await owner.patch(f"/users/{seeded['admin@example.com']}",
                              json={"movement_access": True})).status_code == 200
    admin = await auth(client, "admin@example.com")

    await _set_lockdown(owner, True, sig)
    csv_text = (await admin.get("/movement/export.csv")).text
    for secret in ("Иванов Пётр Сергеевич", "Иванова Мария Петровна", "Директоров", "2015-04-02"):
        assert secret not in csv_text, f"в CSV осталось {secret!r}"
    assert "Ученик " in csv_text and "Представитель " in csv_text

    owner_csv = (await owner.get("/movement/export.csv")).text
    assert "Иванов Пётр Сергеевич" in owner_csv
    assert "2015-04-02" in owner_csv

    await _set_lockdown(owner, False, sig)
    csv_text = (await admin.get("/movement/export.csv")).text
    assert "Иванов Пётр Сергеевич" in csv_text


# ------------------------------------------------------------- журнал событий
async def test_audit_changes_masked_under_lockdown(client, seeded, db_session):
    """`changes` — снимок полей на момент действия: ФИО, телефон, e-mail, логин.

    Обезличивание выполняется на ЧТЕНИИ: содержимое БД остаётся неизменным
    (журнал — юридически значимый след), поэтому после выключения локдауна
    админ снова видит исходные значения.
    """
    from app.models.audit import AuditLog
    from tests.test_totp_pep import sign

    changes = {
        "full_name": {"from": "Иванов Иван Иванович", "to": "Петров Пётр Петрович"},
        "phone": "+7 900 123-45-67",
        "email": "petrov@school.ru",
        "login": "petrov",
        "position": "Учитель физики",
        "student": "Сидоров Пётр",
    }
    entry = AuditLog(
        actor_id=uuid.UUID(seeded["owner@example.com"]),
        actor_email="owner@example.com",
        actor_name="Владелец Один",
        actor_role="OWNER",
        action="USER_UPDATE",
        entity_type="user",
        entity_id=uuid.UUID(seeded["teacher1@example.com"]),
        changes=dict(changes),
        ip_address="10.8.0.15",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = str(entry.id)

    owner, sig = await sign(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")

    async def _row(cli) -> dict:
        items = (await cli.get("/audit")).json()["items"]
        found = [r for r in items if r["id"] == entry_id]
        assert found, "запись журнала не найдена в выдаче"
        return found[0]

    # до локдауна администрация видит исходный снимок
    row = await _row(admin)
    assert row["changes"]["full_name"]["to"] == "Петров Пётр Петрович"
    assert row["changes"]["phone"] == "+7 900 123-45-67"

    await _set_lockdown(owner, True, sig)

    row = await _row(admin)
    assert row["actor_name"].startswith("Сотрудник ")
    assert row["actor_email"] is None and row["ip_address"] is None
    assert row["changes"]["full_name"]["from"].startswith("Сотрудник ")
    assert row["changes"]["full_name"]["to"].startswith("Сотрудник ")
    assert row["changes"]["phone"] == "скрыто"
    assert row["changes"]["email"] == "скрыто"
    assert row["changes"]["login"].startswith("Сотрудник ")
    assert row["changes"]["student"].startswith("Ученик ")
    # неперсональные поля не маскируются — иначе журнал невозможно читать
    assert row["changes"]["position"] == "Учитель физики"

    # человекочитаемое представление строится из того же снимка: имён в нём нет
    text = row["summary"] + "\n" + "\n".join(row["lines"])
    for secret in ("Петров", "Иванов", "Сидоров", "123-45-67", "petrov@school.ru"):
        assert secret not in text, f"в summary/lines осталось {secret!r}"

    # содержимое БД не испорчено маскированием
    db_session.expire_all()
    stored = await db_session.get(AuditLog, uuid.UUID(entry_id))
    assert stored.changes == changes

    # Владелец видит настоящее
    row = await _row(owner)
    assert row["changes"]["full_name"]["to"] == "Петров Пётр Петрович"
    assert row["changes"]["phone"] == "+7 900 123-45-67"
    assert row["actor_name"] == "Владелец Один"

    await _set_lockdown(owner, False, sig)
    row = await _row(admin)
    assert row["changes"]["full_name"]["to"] == "Петров Пётр Петрович"
    assert row["actor_email"] == "owner@example.com"


# ------------------------------------------------------------- уведомления
class _DummyWS:
    """Двойник сокета: перехватывает push-сообщение (см. tests/test_realtime.py)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def test_notifications_masked_under_lockdown(client, seeded, db_session):
    """Уведомления — системный текст с ФИО: «Иванов И. И. отметил(а) задачу…».

    Обезличиваются и в HTTP-выдаче колокольчика, и в мгновенном push'е. В БД текст
    остаётся исходным, Владелец видит настоящие имена.
    """
    from app.services import lockdown, notifications, realtime
    from tests.test_totp_pep import sign

    owner, sig = await sign(client, "owner@example.com")
    teacher_id = uuid.UUID(seeded["teacher1@example.com"])
    owner_id = uuid.UUID(seeded["owner@example.com"])

    async def _push_to(recipient: uuid.UUID, title: str, body: str) -> dict:
        ws = _DummyWS()
        realtime.hub.connect(str(recipient), ws)
        try:
            await notifications.push(
                db_session, user_id=recipient, type_=notifications.TASK_ASSIGNED,
                title=title, body=body, entity_type="task", entity_id=uuid.uuid4(),
            )
            await db_session.commit()
        finally:
            realtime.hub.disconnect(str(recipient), ws)
        assert ws.sent, "push-сообщение не доставлено"
        return ws.sent[0]

    title = "Новая задача: журнал за октябрь"
    body = "Постановщик: Учитель Первый"

    # --- до локдауна текст обычный
    msg = await _push_to(teacher_id, title, body)
    assert msg["body"] == body

    await _set_lockdown(owner, True, sig)
    lockdown.invalidate_cache()          # тестовая БД новая: сбрасываем кэш флага и имён

    # --- учителю приходит обезличенный push
    msg = await _push_to(teacher_id, title, body)
    assert "Учитель Первый" not in (msg["body"] or "")
    assert msg["body"].startswith("Постановщик: Сотрудник ")
    assert msg["title"] == title          # в заголовке имени нет — не меняется

    # --- Владельцу приходит исходный текст
    msg = await _push_to(owner_id, title, body)
    assert msg["body"] == body

    # --- колокольчик: тот же текст, но обезличенный для не-Владельца
    teacher = await auth(client, "teacher1@example.com")
    items = (await teacher.get("/notifications")).json()["items"]
    assert items
    assert all("Учитель Первый" not in (n["body"] or "") for n in items)
    assert any((n["body"] or "").startswith("Постановщик: Сотрудник ") for n in items)

    owner_items = (await owner.get("/notifications")).json()["items"]
    assert any(n["body"] == body for n in owner_items)

    # --- содержимое БД не испорчено
    db_session.expire_all()
    from app.models.notification import Notification
    stored = (await db_session.execute(select(Notification).where(Notification.user_id == teacher_id))).scalars().all()
    assert any(n.body == body for n in stored)

    await _set_lockdown(owner, False, sig)
    lockdown.invalidate_cache()
    items = (await teacher.get("/notifications")).json()["items"]
    assert any(n["body"] == body for n in items)
