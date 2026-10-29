"""Модуль «Замещения» (модуль 3 ТЗ).

Проверяет:
  * создание администрацией: отсутствующий -> замещающий, причина, период;
  * передача объектов — ТОЛЬКО после подтверждения замещающим (v1.11);
  * возврат переданного отсутствующему при отмене, завершении и удалении строки;
  * продление периода (больничный продлили);
  * подтверждение замещающим простым кликом (и запрет подтверждения посторонним);
  * журнал: целиком — администрация/Владелец/кадровик, сотруднику — только свои;
  * удаление строк — Владелец и кадровик (грант `substitutions_access`), не администрация;
  * RBAC: учитель создать не может; CSV-экспорт сотруднику недоступен.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import DEMO_PASSWORD, auth

pytestmark = pytest.mark.asyncio


def sub_payload(absent_id, substitute_id, **kw):
    now = datetime.now(timezone.utc)
    return {
        "absent_user_id": absent_id,
        "substitute_user_id": substitute_id,
        "reason": kw.pop("reason", "Больничный лист"),
        "start_date": kw.pop("start", now - timedelta(hours=1)).isoformat(),
        "end_date": kw.pop("end", now + timedelta(days=5)).isoformat(),
        "transfer_tasks": kw.pop("transfer_tasks", False),
        "transfer_plans": kw.pop("transfer_plans", False),
        "transfer_vshk": kw.pop("transfer_vshk", False),
    }


async def _task_for(client, admin, assignee_id, title="Подготовить отчёт по успеваемости"):
    """Открытая задача на сотрудника — объект для передачи по замещению."""
    resp = await admin.post("/tasks", json={
        "title": title,
        "description": "По итогам четверти",
        "assignee_ids": [assignee_id],
        "priority": "NORMAL",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _assignees(client, admin, task_id) -> set[str]:
    moved = (await admin.get(f"/tasks/{task_id}")).json()
    return {u["user"]["id"] for u in moved["assignees"]}


async def test_rbac_create_and_journal(client, seeded):
    t = await auth(client, "teacher1@example.com")
    resp = await t.post("/substitutions", json=sub_payload(seeded["teacher_2"], seeded["teacher"]))
    assert resp.status_code == 403, resp.text  # учителя не создают замещения

    a = await auth(client, "admin@example.com")
    created = await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher_2"]))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "ACTIVE"
    assert body["absent_user"]["id"] == seeded["teacher"]
    assert body["substitute_user"]["id"] == seeded["teacher_2"]
    # v1.11: при создании объекты не трогаем — ждём подтверждения замещающего
    assert body["transferred_at"] is None
    assert body["note"] == "без передачи объектов"  # флаги передачи не включены

    # валидация
    same = await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher"]))
    assert same.status_code == 400, same.text
    bad_period = await a.post("/substitutions", json={
        **sub_payload(seeded["teacher_2"], seeded["teacher"]),
        "end_date": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
    })
    assert bad_period.status_code == 400, bad_period.text

    # пересечение активных замещений одного человека запрещено
    dup = await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher_2"]))
    assert dup.status_code == 409, dup.text

    # участник замещения видит в журнале ТОЛЬКО свои строки (нужно для подтверждения)
    subst = await auth(client, "teacher2@example.com")
    own = await subst.get("/substitutions")
    assert own.status_code == 200, own.text
    rows = own.json()
    assert [s["id"] for s in rows] == [body["id"]]

    # у участника появляется capability на раздел (иначе он не откроет страницу
    # и не сможет подтвердить принятие)
    caps = (await subst.get("/auth/me")).json()["capabilities"]
    assert "substitutions:read" in caps

    # CSV-экспорт журнала — только администрация/Владелец/кадровик
    denied_csv = await subst.get("/substitutions/export.csv")
    assert denied_csv.status_code == 403, denied_csv.text

    # журнал администрации показывает всё; фильтры по статусу
    admin_list = await a.get("/substitutions?status=ACTIVE")
    assert any(s["id"] == body["id"] for s in admin_list.json())
    none_done = await a.get("/substitutions?status=ENDED")
    assert all(s["status"] == "ENDED" for s in none_done.json())


async def test_stranger_teacher_sees_nothing(client, seeded):
    """Сотрудник без замещений: журнал пуст, capability не выдаётся, экспорт закрыт."""
    owner = await auth(client, "owner@example.com")
    created = await owner.post("/users", json={
        "email": "third@example.com", "full_name": "Третий Пётр Сергеевич",
        "role": "TEACHER", "password": DEMO_PASSWORD,
    })
    assert created.status_code in (200, 201), created.text

    a = await auth(client, "admin@example.com")
    sub = (await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher_2"]))).json()

    third = await auth(client, "third@example.com")
    rows = (await third.get("/substitutions")).json()
    assert rows == []
    caps = (await third.get("/auth/me")).json()["capabilities"]
    assert "substitutions:read" not in caps
    assert (await third.get("/substitutions/export.csv")).status_code == 403
    # чужую строку нельзя ни открыть, ни подтвердить
    assert (await third.get(f"/substitutions/{sub['id']}/confirm", )).status_code in (403, 405)


async def test_confirm_by_substitute_only(client, seeded):
    a = await auth(client, "admin@example.com")
    sub = (await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher_2"]))).json()

    # подтвердить может только замещающий — простым кликом (без тела)
    other = await auth(client, "teacher1@example.com")
    denied = await other.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert denied.status_code == 403, denied.text

    subst = await auth(client, "teacher2@example.com")
    ok = await subst.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "CONFIRMED" and ok.json()["confirmed_at"]
    # флаги передачи не включены — передавать нечего
    assert ok.json()["transferred_at"] is None
    assert ok.json()["note"] == "без передачи объектов"

    # повторное подтверждение — конфликт
    again = await subst.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert again.status_code == 409, again.text

    # завершение — администрация; закрытое нельзя закрыть повторно
    ended = await a.post(f"/substitutions/{sub['id']}/end")
    assert ended.status_code == 200 and ended.json()["status"] == "ENDED"
    teacher_end = await a.post(f"/substitutions/{sub['id']}/end")
    assert teacher_end.status_code == 409, teacher_end.text


async def test_transfer_after_confirm_and_return_on_cancel(client, seeded):
    """Главный сценарий v1.11: передача — после подтверждения, возврат — при отмене."""
    a = await auth(client, "admin@example.com")
    task = await _task_for(client, a, seeded["teacher"])

    sub = (await a.post("/substitutions", json={
        **sub_payload(seeded["teacher"], seeded["teacher_2"]),
        "transfer_tasks": True,
    })).json()

    # ДО подтверждения задача остаётся у отсутствующего
    assert await _assignees(client, a, task["id"]) == {seeded["teacher"]}

    subst = await auth(client, "teacher2@example.com")
    confirmed = await subst.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["transferred_at"], "после подтверждения передача зафиксирована"
    assert "передано задач: 1" in body["note"]

    # исполнитель заменён
    assert await _assignees(client, a, task["id"]) == {seeded["teacher_2"]}

    # запись в истории задачи о переназначении
    moved = (await a.get(f"/tasks/{task['id']}")).json()
    history = moved.get("history") or (await a.get(f"/tasks/{task['id']}/history")).json()
    assert any("замещ" in str(h).lower() for h in history), history

    # уведомление замещающему
    notes = (await subst.get("/notifications")).json()
    items = notes if isinstance(notes, list) else notes.get("items", [])
    assert any("отчёт по успеваемости" in str(n.get("title", "")) + str(n.get("body", "")) for n in items)

    # сводка передачи попадает в CSV-экспорт
    csv_resp = await a.get("/substitutions/export.csv")
    assert csv_resp.status_code == 200
    text = csv_resp.content.decode("utf-8-sig")
    assert "Передать задачи" in text.splitlines()[0]
    assert "Больничный лист" in text
    assert "передано задач: 1" in text

    # ОТМЕНА — задачи возвращаются отсутствующему
    cancelled = await a.post(f"/substitutions/{sub['id']}/cancel", json={"comment": "сотрудник вышел раньше"})
    assert cancelled.status_code == 200, cancelled.text
    cbody = cancelled.json()
    assert cbody["status"] == "CANCELLED"
    assert cbody["transferred_at"] is None, "после возврата объекты больше не у замещающего"
    assert "сотрудник вышел раньше" in (cbody["note"] or "")
    assert "возвращено задач: 1" in (cbody["note"] or "")
    assert await _assignees(client, a, task["id"]) == {seeded["teacher"]}

    # после отмены можно создать новое замещение на тот же период
    new = await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher"]))
    assert new.status_code == 400  # совпадающие участники
    new2 = await a.post("/substitutions", json=sub_payload(seeded["teacher"], seeded["teacher_2"]))
    assert new2.status_code == 201, new2.text


async def test_end_returns_objects(client, seeded):
    """Завершение (вручную и по истечении периода) тоже возвращает объекты."""
    a = await auth(client, "admin@example.com")
    task = await _task_for(client, a, seeded["teacher"])
    sub = (await a.post("/substitutions", json={
        **sub_payload(seeded["teacher"], seeded["teacher_2"]),
        "transfer_tasks": True,
    })).json()

    subst = await auth(client, "teacher2@example.com")
    await subst.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert await _assignees(client, a, task["id"]) == {seeded["teacher_2"]}

    ended = await a.post(f"/substitutions/{sub['id']}/end")
    assert ended.status_code == 200, ended.text
    assert ended.json()["transferred_at"] is None
    assert "возвращено задач: 1" in (ended.json()["note"] or "")
    assert await _assignees(client, a, task["id"]) == {seeded["teacher"]}


async def test_auto_expire_returns_objects(client, seeded, session_factory):
    """Домашняя работа закрывает просроченное замещение и возвращает задачи."""
    from app.services import substitutions as service

    a = await auth(client, "admin@example.com")
    task = await _task_for(client, a, seeded["teacher"])
    now = datetime.now(timezone.utc)
    sub = (await a.post("/substitutions", json={
        **sub_payload(seeded["teacher"], seeded["teacher_2"], start=now - timedelta(days=3),
                      end=now - timedelta(days=2)),
        "transfer_tasks": True,
    })).json()
    subst = await auth(client, "teacher2@example.com")
    await subst.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert await _assignees(client, a, task["id"]) == {seeded["teacher_2"]}

    # имитируем ночной прогон housekeeping
    async with session_factory() as db:
        closed = await service.auto_expire(db)
        await db.commit()
    assert closed >= 1

    assert await _assignees(client, a, task["id"]) == {seeded["teacher"]}
    row = (await a.get("/substitutions?status=ENDED")).json()
    assert any(s["id"] == sub["id"] and s["transferred_at"] is None for s in row)


async def test_extend_period(client, seeded):
    """Больничный продлили: период сдвигается, объекты не трогаются."""
    a = await auth(client, "admin@example.com")
    now = datetime.now(timezone.utc)
    sub = (await a.post("/substitutions", json={
        **sub_payload(seeded["teacher"], seeded["teacher_2"], end=now + timedelta(days=3)),
        "transfer_tasks": True,
    })).json()

    # продлевает администрация
    t = await auth(client, "teacher1@example.com")
    denied = await t.post(f"/substitutions/{sub['id']}/extend",
                          json={"end_date": (now + timedelta(days=9)).isoformat()})
    assert denied.status_code == 403, denied.text

    new_end = now + timedelta(days=12)
    ok = await a.post(f"/substitutions/{sub['id']}/extend",
                      json={"end_date": new_end.isoformat(), "comment": "больничный продлён"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["end_date"].startswith(new_end.strftime("%Y-%m-%d"))
    assert body["status"] == "ACTIVE"
    assert "продлено до" in (body["note"] or "") and "больничный продлён" in (body["note"] or "")

    # назад и в тот же день — нельзя; раньше начала — нельзя
    back = await a.post(f"/substitutions/{sub['id']}/extend",
                        json={"end_date": (now + timedelta(days=1)).isoformat()})
    assert back.status_code == 400, back.text
    past = await a.post(f"/substitutions/{sub['id']}/extend",
                        json={"end_date": (now - timedelta(days=1)).isoformat()})
    assert past.status_code == 400, past.text

    # замещающий получил уведомление о продлении
    subst = await auth(client, "teacher2@example.com")
    notes = (await subst.get("/notifications")).json()
    items = notes if isinstance(notes, list) else notes.get("items", [])
    assert any("продлено" in str(n.get("title", "")).lower() for n in items), items


async def test_delete_by_owner_and_hr_only(client, seeded):
    """Строку замещения удаляет Владелец или назначенный им кадровик (не администрация)."""
    owner = await auth(client, "owner@example.com")
    a = await auth(client, "admin@example.com")
    task = await _task_for(client, a, seeded["teacher"])
    sub = (await a.post("/substitutions", json={
        **sub_payload(seeded["teacher"], seeded["teacher_2"]),
        "transfer_tasks": True,
    })).json()

    # администрация без гранта — не удаляет
    denied = await a.request("DELETE", f"/substitutions/{sub['id']}")
    assert denied.status_code == 403, denied.text
    # замещающий — тоже
    subst = await auth(client, "teacher2@example.com")
    assert (await subst.request("DELETE", f"/substitutions/{sub['id']}")).status_code == 403

    # Владелец назначает кадровика (должность значения не имеет — здесь учитель)
    granted = await owner.patch(f"/users/{seeded['teacher_2']}", json={"substitutions_access": True})
    assert granted.status_code == 200, granted.text
    assert granted.json()["substitutions_access"] is True
    # кадровик видит журнал целиком и получает capability
    hr = await auth(client, "teacher2@example.com")
    assert "substitutions:read" in (await hr.get("/auth/me")).json()["capabilities"]
    assert len((await hr.get("/substitutions")).json()) == 1

    # сначала подтверждение → передача, затем удаление строки кадровиком: задачи возвращаются
    await hr.post(f"/substitutions/{sub['id']}/confirm", json={})
    assert await _assignees(client, a, task["id"]) == {seeded["teacher_2"]}

    deleted = await hr.request("DELETE", f"/substitutions/{sub['id']}")
    assert deleted.status_code == 204, deleted.text
    assert (await a.get("/substitutions")).json() == []
    assert await _assignees(client, a, task["id"]) == {seeded["teacher"]}

    # грант выдаёт только Владелец
    self_grant = await a.patch(f"/users/{seeded['teacher']}", json={"substitutions_access": True})
    assert self_grant.status_code == 403, self_grant.text
