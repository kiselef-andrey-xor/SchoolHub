"""Helpdesk — заявки сотрудников в техническую поддержку школы (v1.12).

Проверяет контракт модуля, спроектированного с нуля:

  * заявку подаёт ЛЮБОЙ сотрудник (роль TEACHER), а видит он только свои;
  * очередь обслуживают системные администраторы — сотрудники с персональным
    грантом Владельца `helpdesk_access`, роль значения не имеет;
  * администрация видит очередь чтением, но НЕ может закрывать заявки;
  * жизненный цикл: NEW → IN_PROGRESS → RESOLVED → CLOSED с подтверждением
    автора и возвратом на доработку; отклонение и отзыв;
  * переназначить заявку можно только другому системному администратору;
  * норматив реакции (SLA) копируется в заявку при создании, просрочка реакции
    вычисляется и попадает в сводку и CSV;
  * переписка и служебные отметки образуют единую ленту, служебные не удаляются;
  * вложения (фото поломки) доступны автору и сисадминам;
  * автозакрытие RESOLVED-заявок по истечении срока (housekeeping).

Отдельно проверяется то, что ломается чаще всего при добавлении модуля:
  * маршрут `/helpdesk/stats` не «съедается» маршрутом `/helpdesk/{ticket_id}`;
  * заявки попадают в архив «Файлы школы» (files_backup.collect_references);
  * действие записывается в журнал событий с человекочитаемой подписью.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from tests.conftest import DEMO_PASSWORD, auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio

PRINTER = {
    "title": "Зажевало бумагу в принтере",
    "description": "В кабинете 214 МФУ остановилось на середине печати, на панели ошибка «замятие».",
    "category": "PRINTER",
    "priority": "HIGH",
    "location": "кабинет 214",
    "contact_phone": "2-14",
}

BOARD = {
    "title": "Нет интернета на интерактивной доске",
    "description": "Доска подключена к сети, но браузер не открывает ни один сайт.",
    "category": "NETWORK",
    "priority": "URGENT",
    "location": "кабинет 12",
}


async def grant_sysadmin(client, seeded, key: str, value: bool = True) -> dict:
    """Владелец назначает сотрудника системным администратором (персональный грант)."""
    owner = await auth(client, "owner@example.com")
    resp = await owner.patch(f"/users/{seeded[key]}", json={"helpdesk_access": value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["helpdesk_access"] is value
    return resp.json()


async def notifications(cli, type_: str) -> list[dict]:
    """Мои уведомления указанного типа (эндпоинт отдаёт Page: {items, total, ...})."""
    resp = await cli.get("/notifications", params={"unread_only": "false", "per_page": 100})
    assert resp.status_code == 200, resp.text
    return [n for n in resp.json()["items"] if n["type"] == type_]


async def create(client, email: str, payload: dict) -> dict:
    cli = await auth(client, email)
    resp = await cli.post("/helpdesk", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------- доступы
async def test_teacher_creates_ticket_and_sees_only_own(client, seeded):
    """Любой сотрудник подаёт заявку; чужие заявки ему не видны ни в журнале, ни по id."""
    first = await create(client, "teacher1@example.com", PRINTER)
    assert first["number"] == 1 and first["year"] >= 2020
    assert first["status"] == "NEW"
    assert first["author"]["id"] == seeded["teacher"]
    assert first["assignee"] is None
    assert first["sla_hours"] == 4, "приоритет HIGH → норматив реакции 4 часа"

    second = await create(client, "teacher2@example.com", BOARD)
    assert second["number"] == 2, "нумерация сквозная в пределах года"

    teacher1 = await auth(client, "teacher1@example.com")
    rows = (await teacher1.get("/helpdesk")).json()
    assert [r["id"] for r in rows] == [first["id"]], "учитель видит только свои обращения"

    # чужая заявка по прямому id — 403, а не 404: сам факт существования очереди
    # не секрет, а содержимое чужого обращения читать нельзя
    assert (await teacher1.get(f"/helpdesk/{second['id']}")).status_code == 403

    # срочная заявка стоит выше обычной в журнале сисадмина
    owner = await auth(client, "owner@example.com")
    queue = (await owner.get("/helpdesk")).json()
    assert queue[0]["id"] == second["id"], "срочная заявка должна быть сверху очереди"


async def test_directory_and_stats_routes_are_not_swallowed_by_ticket_id(client, seeded):
    """`/helpdesk/stats` и `/helpdesk/directory` объявлены до `/{ticket_id}`.

    Если порядок маршрутов перепутать, FastAPI попытается разобрать «stats» как
    UUID и вернёт 422 — раздел останется без сводки.
    """
    teacher = await auth(client, "teacher1@example.com")
    directory = await teacher.get("/helpdesk/directory")
    assert directory.status_code == 200, directory.text
    body = directory.json()
    assert {"value": "PRINTER", "label": "принтер / МФУ / сканер"} in body["categories"]
    assert {"value": "DOC_CAMERA", "label": "документ-камера"} in body["categories"]
    assert body["sla_hours"]["URGENT"] == 2

    stats = await teacher.get("/helpdesk/stats")
    assert stats.status_code == 200, stats.text
    assert stats.json()["is_staff"] is False


async def test_grant_gives_queue_access_without_changing_role(client, seeded):
    """Грант Владельца делает учителя системным администратором: роль остаётся TEACHER.

    Сисадмином в школе обычно работает учитель информатики, поэтому доступ к
    очереди обязан даваться персональным назначением, а не ролью.
    """
    await create(client, "teacher1@example.com", PRINTER)

    teacher2 = await auth(client, "teacher2@example.com")
    # до назначения — только свои заявки
    assert (await teacher2.get("/helpdesk")).json() == []
    assert (await teacher2.get("/helpdesk/stats")).json()["is_staff"] is False

    await grant_sysadmin(client, seeded, "teacher_2")

    teacher2 = await auth(client, "teacher2@example.com")
    stats = (await teacher2.get("/helpdesk/stats")).json()
    assert stats["is_staff"] is True and stats["new"] == 1
    queue = (await teacher2.get("/helpdesk")).json()
    assert len(queue) == 1 and queue[0]["title"] == PRINTER["title"]
    staff = (await teacher2.get("/helpdesk/staff")).json()
    assert {u["full_name"] for u in staff} >= {"Владелец Один", "Учитель Второй"}

    # capability выдаётся вместе с грантом — по нему фронт показывает раздел
    me = (await teacher2.get("/auth/me")).json()
    assert "helpdesk:manage" in me["capabilities"]


async def test_grant_is_owner_only(client, seeded):
    """Назначать системных администраторов может только Владелец."""
    admin = await auth(client, "admin@example.com")
    resp = await admin.patch(f"/users/{seeded['teacher']}", json={"helpdesk_access": True})
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"


async def test_admin_sees_queue_but_cannot_operate(client, seeded):
    """Администрация видит очередь (контроль), но не закрывает заявки.

    Иначе завуч «выполнил» бы заявку, которую никто не чинил: право видеть
    намеренно не даёт права закрывать.
    """
    ticket = await create(client, "teacher1@example.com", PRINTER)
    admin = await auth(client, "admin@example.com")

    assert (await admin.get("/helpdesk")).json()[0]["id"] == ticket["id"]
    assert (await admin.get("/helpdesk/stats")).json()["is_staff"] is True

    take = await admin.post(f"/helpdesk/{ticket['id']}/take", json={})
    assert take.status_code == 403, take.text
    resolve = await admin.post(f"/helpdesk/{ticket['id']}/resolve", json={"resolution": "починили"})
    assert resolve.status_code == 403
    reject = await admin.post(f"/helpdesk/{ticket['id']}/reject", json={"reason": "не наш профиль"})
    assert reject.status_code == 403

    me = (await admin.get("/auth/me")).json()
    assert "helpdesk:read" in me["capabilities"] and "helpdesk:manage" not in me["capabilities"]


# ------------------------------------------------------------- жизненный цикл
async def test_full_lifecycle_take_resolve_confirm(client, seeded):
    """NEW → IN_PROGRESS → RESOLVED → CLOSED с подтверждением автора."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")

    taken = await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert body["status"] == "IN_PROGRESS"
    assert body["assignee"]["id"] == seeded["teacher_2"]
    assert body["taken_at"] is not None
    assert body["reaction_minutes"] is not None and body["reaction_minutes"] >= 0
    # служебная отметка попала в ленту
    assert any(m["kind"] == "SYSTEM" and "взял" in m["body"] for m in body["messages"])

    # взять повторно — конфликт, а не молчаливый успех
    again = await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    assert again.status_code == 409, again.text

    resolved = await sysadmin.post(f"/helpdesk/{tid}/resolve",
                                   json={"resolution": "Устранил замятие, прочистил ролики подачи."})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "RESOLVED" and body["resolved_at"] is not None
    assert body["resolution"].startswith("Устранил замятие")

    author = await auth(client, "teacher1@example.com")
    confirmed = await author.post(f"/helpdesk/{tid}/confirm", json={"comment": "Всё печатает, спасибо"})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "CLOSED"
    assert body["closed_by"]["id"] == seeded["teacher"]
    assert body["auto_closed"] is False
    assert body["work_minutes"] is not None

    # закрытая заявка терминальна: ни взять в работу, ни подтвердить ещё раз
    assert (await sysadmin.post(f"/helpdesk/{tid}/take", json={})).status_code == 409
    assert (await author.post(f"/helpdesk/{tid}/confirm", json={})).status_code == 409
    # в закрытую заявку нельзя писать
    assert (await author.post(f"/helpdesk/{tid}/messages", json={"body": "ещё вопрос"})).status_code == 409


async def test_author_reopens_unresolved_ticket(client, seeded):
    """Автор вернул «выполненную» заявку: статус IN_PROGRESS, решение очищено."""
    ticket = await create(client, "teacher1@example.com", BOARD)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    await sysadmin.post(f"/helpdesk/{tid}/resolve", json={"resolution": "Перезагрузил роутер"})

    author = await auth(client, "teacher1@example.com")
    assert (await author.post(f"/helpdesk/{tid}/reopen", json={"comment": "x"})).status_code == 422
    reopened = await author.post(f"/helpdesk/{tid}/reopen",
                                 json={"comment": "Интернет появился на минуту и снова пропал"})
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == "IN_PROGRESS"
    assert body["resolution"] is None and body["resolved_at"] is None
    assert body["assignee"]["id"] == seeded["teacher_2"], "заявка осталась у прежнего исполнителя"
    assert any(m["body"].startswith("Интернет появился") for m in body["messages"] if m["kind"] == "MESSAGE")

    # вернуть на доработку может только автор
    assert (await sysadmin.post(f"/helpdesk/{tid}/reopen", json={"comment": "не работает"})).status_code == 403


async def test_reject_and_cancel(client, seeded):
    """Отклонение обязательно с причиной; отзыв — право автора."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")

    assert (await sysadmin.post(f"/helpdesk/{tid}/reject", json={"reason": ""})).status_code == 422
    rejected = await sysadmin.post(
        f"/helpdesk/{tid}/reject",
        json={"reason": "Нужна закупка картриджа — оформите служебную записку на имя директора"},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "REJECTED" and body["rejected_reason"].startswith("Нужна закупка")
    assert (await sysadmin.post(f"/helpdesk/{tid}/take", json={})).status_code == 409

    # отзыв второй заявки
    other = await create(client, "teacher2@example.com", BOARD)
    sysadmin2 = await auth(client, "teacher2@example.com")
    cancelled = await sysadmin2.post(f"/helpdesk/{other['id']}/cancel", json={"comment": "Само заработало"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    # чужую заявку учитель БЕЗ гранта отозвать не может (сисадмин — может: дубликат)
    third = await create(client, "teacher1@example.com", PRINTER)
    plain_teacher = await auth(client, "teacher2@example.com")
    await grant_sysadmin(client, seeded, "teacher_2", value=False)
    assert (await plain_teacher.post(f"/helpdesk/{third['id']}/cancel", json={})).status_code == 403
    # а системный администратор отзывает чужой дубликат
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    assert (await sysadmin.post(f"/helpdesk/{third['id']}/cancel",
                               json={"comment": "Дубликат заявки №1"})).status_code == 200


async def test_reassign_only_to_sysadmin(client, seeded):
    """Переназначение — только внутри круга сисадминов."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")

    # учителю без гранта передать нельзя
    bad = await sysadmin.post(f"/helpdesk/{tid}/take", json={"assignee_id": seeded["teacher"]})
    assert bad.status_code == 400, bad.text

    owner_id = seeded["owner"]
    ok = await sysadmin.post(f"/helpdesk/{tid}/take", json={"assignee_id": owner_id,
                                                            "comment": "Нужен доступ к серверной"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["assignee"]["id"] == owner_id
    # заявка ещё не была в работе: назначение другому — это первичное взятие,
    # поэтому в ленте отметка о назначении исполнителя
    assert any("назначена" in m["body"] for m in body["messages"] if m["kind"] == "SYSTEM"), body["messages"]


# ------------------------------------------------------------------ SLA и сводка
async def test_sla_is_copied_at_creation_and_breach_is_computed(client, seeded, session_factory):
    """Норматив фиксируется при создании; просрочка реакции вычисляется при чтении.

    Просрочка не хранится в БД: флаг устаревал бы сам по себе и требовал cron'а.
    """
    ticket = await create(client, "teacher1@example.com", BOARD)   # URGENT → 2 часа
    tid = ticket["id"]

    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.helpdesk import HelpdeskTicket
        from app.services import helpdesk as service

        row = (await session.execute(select(HelpdeskTicket).where(HelpdeskTicket.id == uuid.UUID(tid)))).scalar_one()
        assert row.sla_hours == 2
        assert service.is_sla_breached(row) is False

        # «прокручиваем» время на три часа назад: заявка создана давно, реакции нет
        row.created_at = row.created_at - timedelta(hours=3)
        await session.commit()

    sysadmin = await auth(client, "owner@example.com")
    body = (await sysadmin.get(f"/helpdesk/{tid}")).json()
    assert body["sla_breached"] is True
    assert body["sla_deadline"] is not None
    stats = (await sysadmin.get("/helpdesk/stats")).json()
    assert stats["sla_breached"] == 1 and stats["new"] == 1

    overdue = (await sysadmin.get("/helpdesk", params={"overdue": "true"})).json()
    assert [t["id"] for t in overdue] == [tid]

    # после взятия в работу реакция состоялась — просрочки нет
    await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    body = (await sysadmin.get(f"/helpdesk/{tid}")).json()
    assert body["sla_breached"] is False and (await sysadmin.get("/helpdesk/stats")).json()["sla_breached"] == 0


async def test_sla_breach_notifies_sysadmins_once(client, seeded, session_factory):
    """Напоминание о просрочке уходит сисадминам ОДИН раз на заявку."""
    ticket = await create(client, "teacher1@example.com", BOARD)

    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.helpdesk import HelpdeskTicket
        from app.services import helpdesk as service

        row = (await session.execute(
            select(HelpdeskTicket).where(HelpdeskTicket.id == uuid.UUID(ticket["id"]))
        )).scalar_one()
        row.created_at = row.created_at - timedelta(hours=5)
        await session.commit()
        assert await service.notify_sla_breach(session) == 1
        assert await service.notify_sla_breach(session) == 0, "повторное напоминание не отправляется"
        await session.commit()

    owner = await auth(client, "owner@example.com")
    assert len(await notifications(owner, "TICKET_SLA_BREACH")) == 1


# ------------------------------------------------------------------ переписка
async def test_messages_and_system_entries_are_immutable(client, seeded):
    """Реплики видны обеим сторонам; служебные отметки удалить нельзя никому."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    author = await auth(client, "teacher1@example.com")

    posted = await author.post(f"/helpdesk/{tid}/messages", json={"body": "Бумага застряла в лотке 2"})
    assert posted.status_code == 201, posted.text
    assert posted.json()["kind"] == "MESSAGE"

    reply = await sysadmin.post(f"/helpdesk/{tid}/messages", json={"body": "Иду, откройте кабинет"})
    assert reply.status_code == 201

    detail = (await author.get(f"/helpdesk/{tid}")).json()
    texts = [m["body"] for m in detail["messages"]]
    assert "Бумага застряла в лотке 2" in texts and "Иду, откройте кабинет" in texts
    assert detail["last_message_at"] is not None

    system = next(m for m in detail["messages"] if m["kind"] == "SYSTEM")
    assert (await author.delete(f"/helpdesk/{tid}/messages/{system['id']}")).status_code == 409
    assert (await sysadmin.delete(f"/helpdesk/{tid}/messages/{system['id']}")).status_code == 409

    # чужую реплику удалить нельзя, свою — можно
    assert (await sysadmin.delete(f"/helpdesk/{tid}/messages/{posted.json()['id']}")).status_code == 403
    assert (await author.delete(f"/helpdesk/{tid}/messages/{posted.json()['id']}")).status_code == 200

    # пустое сообщение отклоняется: схема пропускает пробелы (min_length=1),
    # а сервис возвращает 400 с русской подсказкой
    resp = await author.post(f"/helpdesk/{tid}/messages", json={"body": "   "})
    assert resp.status_code == 400 and "Пустое сообщение" in resp.json()["detail"]


# ------------------------------------------------------------------- вложения
async def test_attachments_visible_to_author_and_staff(client, seeded):
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    author = await auth(client, "teacher1@example.com")
    up = await author.post(f"/helpdesk/{tid}/files", files=upload("jam.pdf", pdf_bytes("замятие"), "application/pdf"))
    assert up.status_code == 201, up.text
    files = up.json()["files"]
    assert len(files) == 1
    assert files[0]["kind"] == "AUTHOR"
    # оригинал уже PDF: копия не нужна, статус NA, но открыть инлайн можно
    assert files[0]["pdf_status"] == "NA"
    assert files[0]["pdf_ready"] is False

    fid = files[0]["id"]
    original = await author.get(f"/helpdesk/{tid}/files/{fid}/download")
    assert original.status_code == 200 and original.content.startswith(b"%PDF")
    inline = await author.get(f"/helpdesk/{tid}/files/{fid}/download", params={"inline": "true"})
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("application/pdf")
    assert "inline" in inline.headers["content-disposition"]

    # учитель без гранта чужую заявку и её файлы не видит
    stranger = await auth(client, "teacher2@example.com")
    assert (await stranger.get(f"/helpdesk/{tid}/files/{fid}/download")).status_code == 403

    # к закрытой заявке файлы не добавляются
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    await sysadmin.post(f"/helpdesk/{tid}/resolve", json={"resolution": "Устранил замятие"})
    await author.post(f"/helpdesk/{tid}/confirm", json={})
    assert (await author.post(f"/helpdesk/{tid}/files",
                              files=upload("x.pdf", pdf_bytes(), "application/pdf"))).status_code == 409

    # администратор прикладывает акт — вложение помечается STAFF
    second = await create(client, "teacher1@example.com", BOARD)
    up2 = await sysadmin.post(f"/helpdesk/{second['id']}/files",
                              files=upload("akt.pdf", pdf_bytes("акт"), "application/pdf"))
    assert up2.status_code == 201, up2.text
    assert up2.json()["files"][0]["kind"] == "STAFF"


async def test_files_are_collected_for_backup(client, seeded, session_factory):
    """Вложения заявок обязаны попадать в архив «Файлы школы» под исходным именем."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    author = await auth(client, "teacher1@example.com")
    up = await author.post(f"/helpdesk/{ticket['id']}/files",
                           files=upload("jam.pdf", pdf_bytes("замятие"), "application/pdf"))
    assert up.status_code == 201

    from app.services.files_backup import collect_references

    async with session_factory() as session:
        refs = await collect_references(session)
    kinds = {r["kind"] for r in refs}
    assert "helpdesk_file" in kinds
    assert any(r["original_name"] == "jam.pdf" for r in refs)


# ------------------------------------------------------------------- прочее
async def test_update_ticket_and_export_csv(client, seeded):
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    author = await auth(client, "teacher1@example.com")

    patched = await author.patch(f"/helpdesk/{tid}", json={"location": "кабинет 214, второй корпус",
                                                           "priority": "URGENT"})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["location"].endswith("второй корпус")
    assert body["sla_hours"] == 2, "норматив пересчитан по новому приоритету (заявка ещё не взята)"

    # чужой автор править не может
    stranger = await auth(client, "teacher2@example.com")
    assert (await stranger.patch(f"/helpdesk/{tid}", json={"title": "Другая тема"})).status_code == 403

    # Выгрузка журнала — отчётность того, кто видит очередь целиком (сисадмины и
    # администрация), как у CSV-экспорта замещений. Автора-учителя она не касается:
    # свои две заявки он видит в журнале, а кнопка «Экспорт CSV» ему не рисуется —
    # контракт интерфейса обязан соблюдаться и на сервере.
    await create(client, "teacher2@example.com", BOARD)
    forbidden_export = await author.get("/helpdesk/export.csv")
    assert forbidden_export.status_code == 403, forbidden_export.text

    owner = await auth(client, "owner@example.com")
    exported = await owner.get("/helpdesk/export.csv")
    assert exported.status_code == 200, exported.text
    text = exported.content.decode("utf-8-sig")
    header = text.splitlines()[0].split(";")
    assert header[0] == "№" and "Норматив (ч)" in header and "Просрочена реакция" in header
    assert "Зажевало бумагу в принтере" in text
    assert "кабинет 214, второй корпус" in text
    # Владелец видит очередь целиком, включая чужие заявки
    assert "Нет интернета на интерактивной доске" in text

    # системному администратору с грантом Владельца выгрузка доступна наравне с Владельцем
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    staff_rows = (await sysadmin.get("/helpdesk/export.csv")).content.decode("utf-8-sig")
    assert "Зажевало бумагу в принтере" in staff_rows

    # администрация без гранта видит очередь чтением — значит, и выгрузку получает
    admin = await auth(client, "admin@example.com")
    assert (await admin.get("/helpdesk/export.csv")).status_code == 200


async def test_owner_deletes_ticket(client, seeded):
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    author = await auth(client, "teacher1@example.com")
    assert (await author.delete(f"/helpdesk/{tid}")).status_code == 403

    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    assert (await sysadmin.delete(f"/helpdesk/{tid}")).status_code == 403

    owner = await auth(client, "owner@example.com")
    assert (await owner.delete(f"/helpdesk/{tid}")).status_code == 204
    assert (await owner.get(f"/helpdesk/{tid}")).status_code == 404


async def test_autoclose_resolved_tickets(client, seeded, session_factory):
    """Housekeeping закрывает выполненные заявки, которые автор не подтвердил."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    await sysadmin.post(f"/helpdesk/{tid}/resolve", json={"resolution": "Заменил картридж"})

    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.helpdesk import HelpdeskTicket
        from app.services import helpdesk as service

        row = (await session.execute(select(HelpdeskTicket).where(HelpdeskTicket.id == uuid.UUID(tid)))).scalar_one()
        assert await service.autoclose_resolved(session) == 0, "свежая заявка ещё не закрывается"
        row.resolved_at = row.resolved_at - timedelta(days=service._autoclose_days() + 1)
        await session.commit()
        assert await service.autoclose_resolved(session) == 1
        await session.commit()
        await session.refresh(row)
        assert row.status.value == "CLOSED" and row.auto_closed is True and row.closed_by_id is None
        assert any("автоматически" in m.body for m in row.messages)


async def test_ticket_appears_in_audit_journal(client, seeded):
    """Каждое действие по заявке пишется в журнал событий с русской подписью."""
    await create(client, "teacher1@example.com", PRINTER)
    owner = await auth(client, "owner@example.com")
    rows = (await owner.get("/audit", params={"entity_type": "helpdesk_ticket", "per_page": 50})).json()
    items = rows["items"]
    assert "TICKET_CREATE" in {r["action"] for r in items}
    created = next(r for r in items if r["action"] == "TICKET_CREATE")
    # человекочитаемая подпись собирается audit_human.humanize прямо в ответе журнала
    assert created["summary"] == "Подана заявка в техподдержку", created["summary"]
    assert any("Зажевало бумагу" in line for line in created["lines"]), created["lines"]


async def test_ticket_in_global_search(client, seeded):
    """Заявка находится глобальным поиском — в пределах видимости автора."""
    await create(client, "teacher1@example.com", PRINTER)
    author = await auth(client, "teacher1@example.com")
    found = (await author.get("/search", params={"q": "зажевало"})).json()
    assert any("Зажевало бумагу" in item["title"] for item in found["helpdesk"])
    assert found["helpdesk"][0]["url"].startswith("/helpdesk?t=")

    stranger = await auth(client, "teacher2@example.com")
    assert (await stranger.get("/search", params={"q": "зажевало"})).json()["helpdesk"] == []


async def test_ticket_notifications_reach_author(client, seeded):
    """Автор получает уведомления о взятии в работу и о выполнении."""
    ticket = await create(client, "teacher1@example.com", PRINTER)
    tid = ticket["id"]
    await grant_sysadmin(client, seeded, "teacher_2")
    sysadmin = await auth(client, "teacher2@example.com")
    await sysadmin.post(f"/helpdesk/{tid}/take", json={})
    await sysadmin.post(f"/helpdesk/{tid}/resolve", json={"resolution": "Готово"})

    author = await auth(client, "teacher1@example.com")
    assert len(await notifications(author, "TICKET_TAKEN")) == 1
    resolved = await notifications(author, "TICKET_RESOLVED")
    assert len(resolved) == 1
    assert resolved[0]["entity_type"] == "helpdesk_ticket"
    assert "Подтвердите закрытие" in resolved[0]["body"]

    # новая заявка адресована всей очереди сисадминов, кроме самого автора
    third = await create(client, "teacher1@example.com", BOARD)
    sysadmin = await auth(client, "teacher2@example.com")
    fresh = await notifications(sysadmin, "TICKET_NEW")
    assert any(n["entity_id"] == third["id"] for n in fresh)


async def test_invalid_payloads_are_rejected(client, seeded):
    """Короткая тема/описание и неизвестная категория не проходят валидацию."""
    teacher = await auth(client, "teacher1@example.com")
    assert (await teacher.post("/helpdesk", json={**PRINTER, "title": "а"})).status_code == 422
    assert (await teacher.post("/helpdesk", json={**PRINTER, "description": "нет"})).status_code == 422
    resp = await teacher.post("/helpdesk", json={**PRINTER, "category": "TELEPORT"})
    assert resp.status_code == 422
    assert (await teacher.post("/helpdesk", json={**PRINTER, "location": "x" * 400})).status_code == 422
    # без аутентификации раздел недоступен
    from tests.conftest import new_client

    anon = new_client(client)
    assert (await anon.get("/helpdesk")).status_code == 401
    assert (await anon.post("/helpdesk", json=PRINTER)).status_code == 401


async def test_helpdesk_access_defaults_to_false_for_existing_users(client, seeded):
    """После обновления никто не становится сисадмином «сам собой»."""
    owner = await auth(client, "owner@example.com")
    rows = (await owner.get("/users", params={"per_page": 100})).json()
    items = rows["items"]
    assert items, "список пользователей пуст"
    assert all(u.get("helpdesk_access") is False for u in items)
    me = (await owner.get("/auth/me")).json()
    assert me["helpdesk_access"] is False
    assert "helpdesk:manage" in me["capabilities"], "Владелец обслуживает очередь без гранта"


async def test_demo_password_still_works_after_grant(client, seeded):
    """Грант не ломает вход: сисадмин логинится тем же паролем."""
    await grant_sysadmin(client, seeded, "teacher_2")
    cli = await auth(client, "teacher2@example.com", DEMO_PASSWORD)
    assert (await cli.get("/auth/me")).json()["helpdesk_access"] is True
