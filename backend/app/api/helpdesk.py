"""API Helpdesk: заявки сотрудников в техническую поддержку школы (v1.12).

Маршруты
--------
    GET    /helpdesk/directory            справочники формы (категории, приоритеты, нормативы)
    GET    /helpdesk/stats                сводка для шапки раздела и дашборда
    GET    /helpdesk                      журнал заявок (фильтры, своя очередь)
    GET    /helpdesk/export.csv           выгрузка журнала
    POST   /helpdesk                      создать заявку (любой сотрудник)
    GET    /helpdesk/{id}                 карточка с лентой и вложениями
    PATCH  /helpdesk/{id}                 уточнить заявку
    POST   /helpdesk/{id}/take            взять в работу / переназначить (сисадмин)
    POST   /helpdesk/{id}/resolve         выполнено (сисадмин)
    POST   /helpdesk/{id}/confirm         автор подтвердил → закрыта
    POST   /helpdesk/{id}/reopen          автор вернул на доработку
    POST   /helpdesk/{id}/reject          отклонить с причиной (сисадмин)
    POST   /helpdesk/{id}/cancel          отозвать (автор)
    DELETE /helpdesk/{id}                 удалить (Владелец)
    POST   /helpdesk/{id}/messages        реплика в переписке
    DELETE /helpdesk/{id}/messages/{mid}  удалить свою реплику
    POST   /helpdesk/{id}/files           вложение (фото поломки, акт)
    GET    /helpdesk/{id}/files/{fid}/download?inline=true   открыть/скачать
    DELETE /helpdesk/{id}/files/{fid}     удалить вложение

Права
-----
Проверяются в сервисе (`app.services.helpdesk`), а не только зависимостью роли:
доступ к очереди даёт персональный грант Владельца, поэтому `require_role` здесь
не подходит. Эндпоинты доступны всем авторизованным, а видимость и действия
ограничивает `can_see()` / `can_operate()`.

Важно про порядок маршрутов: `/helpdesk/directory`, `/helpdesk/stats` и
`/helpdesk/export.csv` объявлены ДО `/helpdesk/{ticket_id}` — иначе FastAPI
попытается разобрать «stats» как UUID и вернёт 422.
"""
from __future__ import annotations

import io
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.csvsafe import safe_writer
from app.enums import PdfStatus
from app.errors import forbidden, not_found
from app.models.helpdesk import HelpdeskTicket
from app.models.user import User
from app.schemas.helpdesk import (
    TicketCloseIn,
    TicketDirectoryOut,
    TicketIn,
    TicketMessageIn,
    TicketMessageOut,
    TicketOut,
    TicketRejectIn,
    TicketReopenIn,
    TicketResolveIn,
    TicketStatsOut,
    TicketTakeIn,
    TicketUpdateIn,
)
from app.security import CurrentUser, SessionDep
from app.services import document_pdf, lockdown
from app.services import helpdesk as service
from app.services import storage as _storage_mod

router = APIRouter(prefix="/helpdesk", tags=["helpdesk"])


def _out(ticket: HelpdeskTicket) -> TicketOut:
    """Карточка заявки + вычисляемые показатели SLA (в БД они не хранятся)."""
    data = TicketOut.model_validate(ticket)
    for key, value in service.metrics(ticket).items():
        setattr(data, key, value)
    data.last_message_at = ticket.messages[-1].created_at if ticket.messages else None
    return data


# ------------------------------------------------------------------ справочники
@router.get("/directory", response_model=TicketDirectoryOut,
            summary="Справочники формы заявки (категории, приоритеты, нормативы реакции)")
async def get_directory() -> TicketDirectoryOut:
    return TicketDirectoryOut(**service.directory())


@router.get("/stats", response_model=TicketStatsOut, summary="Сводка по заявкам (шапка раздела, дашборд)")
async def get_stats(session: SessionDep, actor: CurrentUser) -> TicketStatsOut:
    return TicketStatsOut(**await service.stats(session, actor))


@router.get("/staff", summary="Системные администраторы (для переназначения заявки)")
async def list_staff(session: SessionDep, actor: CurrentUser) -> list[dict]:
    """Круг исполнителей: Владелец + сотрудники с грантом `helpdesk_access`.

    Отдаётся только тем, кто сам в этом круге или в администрации — список
    сисадминов не нужен учителю, ему достаточно подать заявку.
    """
    if not service.can_view_all(actor):
        raise not_found("Список системных администраторов")
    rows = (await session.execute(
        select(User).where(User.is_active.is_(True), User.is_deleted.is_(False))
    )).scalars().all()
    return [
        {"id": str(u.id), "full_name": lockdown.disp(u.full_name, u.id), "role": u.role.value,
         "positions": [p.title for p in (u.positions or [])]}
        for u in rows if service.is_staff(u)
    ]


# --------------------------------------------------------------------- журнал
@router.get("", response_model=list[TicketOut], summary="Журнал заявок")
async def list_tickets(
    session: SessionDep,
    actor: CurrentUser,
    status: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=20),
    priority: str | None = Query(default=None, max_length=10),
    assignee_id: uuid.UUID | None = None,
    author_id: uuid.UUID | None = None,
    mine: bool = False,
    unassigned: bool = False,
    overdue: bool = Query(default=False, description="Только открытые с просроченной реакцией"),
    year: int | None = Query(default=None, ge=2000, le=2200),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[TicketOut]:
    rows = await service.list_tickets(
        session, actor, status=status, category=category, priority=priority,
        assignee_id=assignee_id, author_id=author_id, mine=mine, unassigned=unassigned,
        overdue=overdue, year=year, q=q, limit=limit,
    )
    return [_out(t) for t in rows]


@router.get("/export.csv", summary="Выгрузка журнала заявок в CSV")
async def export_tickets(session: SessionDep, actor: CurrentUser,
                         status: str | None = None, year: int | None = None) -> Response:
    # Выгрузка журнала — инструмент отчётности того, кто видит очередь целиком
    # (сисадмины и администрация), а не сотрудника с его двумя заявками: тот же
    # принцип, что у CSV-экспорта замещений. Видимость сама по себе не ограничила бы
    # выгрузку (учитель получил бы только свои строки), но контракт раздела —
    # кнопка «Экспорт CSV» рисуется лишь тем, кто видит очередь, — обязан
    # соблюдаться и на сервере: иначе API и интерфейс расходятся.
    if not service.can_view_all(actor):
        raise forbidden("Выгрузка журнала заявок доступна системным администраторам и администрации")
    rows = await service.list_tickets(session, actor, status=status, year=year, limit=500)
    buf = io.StringIO()
    w = safe_writer(buf, delimiter=";")
    w.writerow(["№", "Год", "Тема", "Категория", "Приоритет", "Статус", "Автор", "Место",
                "Исполнитель", "Создана", "Взята в работу", "Реакция (мин)", "Выполнена",
                "Закрыта", "Решение", "Норматив (ч)", "Просрочена реакция"])
    for t in rows:
        m = service.metrics(t)
        # ФИО идут через lockdown.disp(): CSV строится мимо Pydantic-схем, поэтому
        # сериализаторы режим «Локдаун» здесь не применят (review/REVIEW.md §P1-1)
        w.writerow([
            t.number or "", t.year or "", t.title,
            service.CATEGORY_RU[service.enum_value(t.category)],
            service.PRIORITY_RU[service.enum_value(t.priority)],
            service.status_label(t.status),
            lockdown.disp(t.author.full_name if t.author else None, t.author_id),
            t.location or "",
            lockdown.disp(t.assignee.full_name if t.assignee else None, t.assignee_id),
            t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else "",
            t.taken_at.strftime("%d.%m.%Y %H:%M") if t.taken_at else "",
            m["reaction_minutes"] if m["reaction_minutes"] is not None else "",
            t.resolved_at.strftime("%d.%m.%Y %H:%M") if t.resolved_at else "",
            t.closed_at.strftime("%d.%m.%Y %H:%M") if t.closed_at else "",
            (t.resolution or t.rejected_reason or "").replace("\n", " "),
            t.sla_hours or "",
            "да" if m["sla_breached"] else "нет",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="helpdesk_tickets.csv"'},
    )


# -------------------------------------------------------------------- создание
@router.post("", response_model=TicketOut, status_code=201,
             summary="Подать заявку в техподдержку (любой сотрудник)")
async def create_ticket(payload: TicketIn, session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.create_ticket(
        session, actor, title=payload.title, description=payload.description,
        category=payload.category, priority=payload.priority,
        location=payload.location, contact_phone=payload.contact_phone,
        equipment_id=payload.equipment_id,
    )
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.get("/{ticket_id}", response_model=TicketOut, summary="Карточка заявки: лента, вложения, сроки")
async def get_ticket(ticket_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    # PDF-копии готовятся фоном: при открытии карточки доделываем то, что ещё висит
    # в PENDING (например, контейнер перезапустился между загрузкой и конверсией)
    await _refresh_pending_pdfs(ticket)
    await session.commit()
    return _out(ticket)


@router.patch("/{ticket_id}", response_model=TicketOut, summary="Уточнить заявку")
async def update_ticket(ticket_id: uuid.UUID, payload: TicketUpdateIn,
                        session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    patch = payload.model_dump(exclude_unset=True)
    ticket = await service.update_ticket(session, actor, ticket, **patch)
    await session.commit()
    return _out(await _reload(session, ticket.id))


# ------------------------------------------------------------- жизненный цикл
@router.post("/{ticket_id}/take", response_model=TicketOut,
             summary="Взять в работу (или переназначить другому системному администратору)")
async def take_ticket(ticket_id: uuid.UUID, payload: TicketTakeIn | None,
                      session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    body = payload or TicketTakeIn()
    await service.take_ticket(session, actor, ticket, assignee_id=body.assignee_id,
                              comment=body.comment)
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.post("/{ticket_id}/resolve", response_model=TicketOut,
             summary="Выполнено (обязательно с описанием того, что сделано)")
async def resolve_ticket(ticket_id: uuid.UUID, payload: TicketResolveIn,
                         session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.resolve_ticket(session, actor, ticket, resolution=payload.resolution,
                                 comment=payload.comment)
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.post("/{ticket_id}/confirm", response_model=TicketOut,
             summary="Автор подтвердил выполнение → заявка закрыта")
async def confirm_ticket(ticket_id: uuid.UUID, payload: TicketCloseIn | None,
                         session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.close_ticket(session, actor, ticket, comment=(payload.comment if payload else None))
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.post("/{ticket_id}/reopen", response_model=TicketOut,
             summary="Вернуть выполненную заявку на доработку (автор)")
async def reopen_ticket(ticket_id: uuid.UUID, payload: TicketReopenIn,
                        session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.reopen_ticket(session, actor, ticket, comment=payload.comment)
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.post("/{ticket_id}/reject", response_model=TicketOut,
             summary="Отклонить заявку с указанием причины (системный администратор)")
async def reject_ticket(ticket_id: uuid.UUID, payload: TicketRejectIn,
                        session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.reject_ticket(session, actor, ticket, reason=payload.reason)
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.post("/{ticket_id}/cancel", response_model=TicketOut,
             summary="Отозвать заявку (автор или системный администратор)")
async def cancel_ticket(ticket_id: uuid.UUID, payload: TicketCloseIn | None,
                        session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.cancel_ticket(session, actor, ticket, comment=(payload.comment if payload else None))
    await session.commit()
    return _out(await _reload(session, ticket.id))


@router.delete("/{ticket_id}", status_code=204, summary="Удалить заявку (Владелец)")
async def delete_ticket(ticket_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.delete_ticket(session, actor, ticket)
    await session.commit()


# ------------------------------------------------------------------ переписка
@router.post("/{ticket_id}/messages", response_model=TicketMessageOut, status_code=201,
             summary="Реплика в переписке по заявке")
async def add_message(ticket_id: uuid.UUID, payload: TicketMessageIn,
                      session: SessionDep, actor: CurrentUser) -> TicketMessageOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    row = await service.add_message(session, actor, ticket, payload.body)
    await session.commit()
    await session.refresh(row)
    return TicketMessageOut.model_validate(row)


@router.delete("/{ticket_id}/messages/{message_id}", response_model=TicketOut,
               summary="Удалить свою реплику (служебные отметки не удаляются)")
async def delete_message(ticket_id: uuid.UUID, message_id: uuid.UUID,
                         session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.delete_message(session, actor, ticket, message_id)
    await session.commit()
    return _out(await _reload(session, ticket.id))


# ------------------------------------------------------------------- вложения
@router.post("/{ticket_id}/files", response_model=TicketOut, status_code=201,
             summary="Прикрепить файл (фото поломки, акт, счёт)")
async def upload_file(ticket_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                      background: BackgroundTasks,
                      file: Annotated[UploadFile, File(description="Файл до 25 МБ")]) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    row = await service.add_file(session, actor, ticket, file)
    await session.commit()
    # PDF-копия готовится ПОСЛЕ ответа: загрузка не должна ждать LibreOffice
    document_pdf.ensure_pdf_background(background, row)
    return _out(await _reload(session, ticket.id))


@router.get("/{ticket_id}/files/{file_id}/download", summary="Скачать вложение (inline=true — открыть в браузере)")
async def download_file(ticket_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep,
                        actor: CurrentUser,
                        inline: bool = Query(default=False,
                                             description="inline — PDF-копия для новой вкладки")):
    ticket = await service.get_ticket(session, ticket_id, actor)
    row = next((f for f in ticket.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    if inline and row.pdf_status == PdfStatus.PENDING:
        # копия ещё не готова (фон не отработал) — доделываем здесь и сейчас
        await document_pdf.ensure_pdf(row)
        await session.commit()
    info = document_pdf.resolve_view(row) if inline else {
        "preview": "download", "mime": row.mime_type or "application/octet-stream",
        "path": row.storage_path, "filename": row.original_name,
    }
    return FileResponse(
        path=str(_storage_mod.storage.abspath(info["path"])),
        media_type=info["mime"],
        filename=info["filename"],
        content_disposition_type="inline" if info["preview"] == "inline" else "attachment",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{ticket_id}/files/{file_id}", response_model=TicketOut, summary="Удалить вложение")
async def delete_file(ticket_id: uuid.UUID, file_id: uuid.UUID,
                      session: SessionDep, actor: CurrentUser) -> TicketOut:
    ticket = await service.get_ticket(session, ticket_id, actor)
    await service.delete_file(session, actor, ticket, file_id)
    await session.commit()
    return _out(await _reload(session, ticket.id))


# ------------------------------------------------------------------- служебное
async def _reload(session: SessionDep, ticket_id: uuid.UUID) -> HelpdeskTicket:
    """Перечитывает заявку: ленты `messages`/`files` загружаются selectin и после
    commit'а в объекте остаются устаревшие списки."""
    ticket = await session.get(HelpdeskTicket, ticket_id)
    if ticket is None:
        raise not_found("Заявка")
    await session.refresh(ticket)
    return ticket


async def _refresh_pending_pdfs(ticket: HelpdeskTicket) -> None:
    """Доделывает PDF-копии, зависшие в PENDING.

    Сценарий реальный: контейнер перезапустили (обновление, сбой) между загрузкой
    файла и фоновой конверсией — BackgroundTasks живёт только в пределах запроса.
    Без доделки документ навсегда остался бы «готовится».
    """
    for row in ticket.files:
        if row.pdf_status == PdfStatus.PENDING:
            await document_pdf.ensure_pdf(row)
