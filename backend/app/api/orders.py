"""API модуля «Приказы».

Доступ:
  * чтение/скачивание/ознакомление — все аутентифицированные (учитель видит только PUBLISHED);
  * создание, редактирование, загрузка файлов, публикация, архив, лист ознакомления — OWNER/ADMIN.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.background import BackgroundTask

from app.csvsafe import safe_writer
from app.enums import AckStatus, OrderStatus, Role
from app.errors import bad_request, forbidden, not_found
from app.models.order import Order, OrderAcknowledgement
from app.models.user import User
from app.schemas.common import Page
from app.schemas.order import (
    AckCreateIn,
    AckListIn,
    AcknowledgementOut,
    OrderCreateIn,
    OrderListOut,
    OrderOut,
    OrderUpdateIn,
)
from app.security import CurrentUser, SessionDep, client_meta, require_role
from app.services import documents, lockdown
from app.services import orders as service
from app.services import storage as _storage_mod
from app.services import totp as totp_service
from app.types import enum_value

router = APIRouter(prefix="/orders", tags=["orders"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]   # ADMIN или OWNER
PageDep = Annotated[int, Query(ge=1)]
PerPageDep = Annotated[int, Query(ge=1, le=100)]


async def _reload_acks(session, order: Order) -> None:
    """Перечитывает лист ознакомления после публикации/смены круга (избегаем lazy-load)."""
    rows = (
        await session.execute(
            select(OrderAcknowledgement)
            .where(OrderAcknowledgement.order_id == order.id)
            .options(selectinload(OrderAcknowledgement.user))
        )
    ).scalars().all()
    order.acknowledgements = list(rows)


# ---------------------------------------------------------------- serializers
def _file_out(f) -> dict:
    return {
        "id": f.id,
        "original_name": f.original_name,
        "mime_type": f.mime_type,
        "size_bytes": f.size_bytes,
        "version": f.version,
        "is_current": bool(f.is_current),
        "checksum": f.checksum,
        "created_at": f.created_at,
        "uploaded_by": f.uploaded_by,
    }


def _order_out(order: Order, actor: User) -> OrderOut:
    current = order.current_file
    return OrderOut(
        id=order.id,
        number=order.number,
        order_year=order.order_year,
        title=order.title,
        description=order.description,
        order_type=order.order_type,
        status=order.status,
        issued_on=order.issued_on,
        ack_deadline=order.ack_deadline,
        require_all=order.require_all,
        recipient_ids=[str(x) for x in (order.recipient_ids or [])],
        signature_policy=order.signature_policy,
        effective_policy=totp_service.effective_policy(order),
        created_at=order.created_at,
        updated_at=order.updated_at,
        published_at=order.published_at,
        created_by=order.created_by,
        files=[_file_out(f) for f in order.files],
        current_file=_file_out(current) if current else None,
        ack_total=order.ack_total,
        ack_done=order.ack_done,
        my_ack=service.my_ack(order, actor),
        can_manage=service.can_manage(actor),
    )


def _order_list_out(order: Order, actor: User) -> OrderListOut:
    current = order.current_file
    return OrderListOut(
        id=order.id,
        number=order.number,
        order_year=order.order_year,
        title=order.title,
        order_type=order.order_type,
        status=order.status,
        issued_on=order.issued_on,
        ack_deadline=order.ack_deadline,
        signature_policy=order.signature_policy,
        effective_policy=totp_service.effective_policy(order),
        published_at=order.published_at,
        created_by=order.created_by,
        current_file=_file_out(current) if current else None,
        ack_total=order.ack_total,
        ack_done=order.ack_done,
        my_ack=service.my_ack(order, actor),
        can_manage=service.can_manage(actor),
    )


# --------------------------------------------------------------------- list
@router.get("", response_model=Page[OrderListOut], summary="Список приказов с фильтрами")
async def list_orders(
    session: SessionDep,
    actor: CurrentUser,
    search: Annotated[str | None, Query(max_length=200, description="Поиск по номеру/заголовку/описанию")] = None,
    order_type: str | None = None,
    status: OrderStatus | None = Query(default=None, description="Только для OWNER/ADMIN"),
    date_from: date | None = None,
    date_to: date | None = None,
    ack: Annotated[str | None, Query(pattern="^(all|pending|acknowledged)$")] = None,
    mine_pending: bool = Query(default=False, description="Только приказы, где я ещё не расписался"),
    page: PageDep = 1,
    per_page: PerPageDep = 20,
) -> Page[OrderListOut]:
    if date_from and date_to and date_from > date_to:
        raise bad_request("Дата «с» больше даты «по»")
    if status is not None and not service.can_manage(actor):
        # явный 403 вместо «тихого игнора»: учитель в принципе не видит неопубликованные приказы
        raise forbidden("Фильтр по статусу доступен только Администрации и Владельцу")

    rows, total = await service.list_orders(
        session,
        actor,
        search=search,
        order_type=order_type,
        status=status,
        date_from=date_from,
        date_to=date_to,
        ack=ack,
        mine_pending=mine_pending,
        page=page,
        per_page=per_page,
    )
    return Page[OrderListOut](
        items=[_order_list_out(o, actor) for o in rows],
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@router.post("", response_model=OrderOut, status_code=201, summary="Создать приказ (черновик)")
async def create_order(payload: OrderCreateIn, session: SessionDep, manager: ManagerDep) -> OrderOut:
    order = await service.create_order(session, manager, payload)
    await session.refresh(order)          # подтянуть eager-связи (files/acknowledgements)
    return _order_out(order, manager)


@router.get("/{order_id}", response_model=OrderOut, summary="Карточка приказа")
async def get_order(order_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> OrderOut:
    order = await service.get_order(session, order_id, actor)
    return _order_out(order, actor)


@router.patch("/{order_id}", response_model=OrderOut, summary="Редактировать приказ")
async def update_order(
    order_id: uuid.UUID, payload: OrderUpdateIn, session: SessionDep, manager: ManagerDep
) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    order = await service.update_order(session, manager, order, payload)
    await session.refresh(order)
    return _order_out(order, manager)


@router.post("/{order_id}/publish", response_model=OrderOut, summary="Опубликовать приказ")
async def publish_order(
    order_id: uuid.UUID,
    session: SessionDep,
    manager: ManagerDep,
    user_ids: list[uuid.UUID] | None = Query(default=None, description="Явный круг ознакомления"),
) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    order = await service.publish_order(session, manager, order, recipient_ids=user_ids)
    await session.flush()
    await session.refresh(order)
    await _reload_acks(session, order)
    return _order_out(order, manager)


@router.post("/{order_id}/archive", response_model=OrderOut, summary="В архив (read-only)")
async def archive_order(order_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    order = await service.archive_order(session, manager, order)
    await session.refresh(order)
    return _order_out(order, manager)


@router.delete(
    "/{order_id}",
    status_code=204,
    summary="Удалить черновик (ADMIN/OWNER) или архивный приказ (только OWNER)",
)
async def delete_order(order_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> None:
    order = await service.get_order(session, order_id, manager)
    await service.delete_order(session, manager, order)


# -------------------------------------------------------------------- files
@router.post("/{order_id}/files", response_model=OrderOut, status_code=201, summary="Загрузить PDF/DOCX")
async def upload_file(
    order_id: uuid.UUID,
    session: SessionDep,
    manager: ManagerDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="PDF или DOCX, до 25 МБ")],
) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    row = await service.save_file_version(session, manager, order, file)
    # DOCX → сразу фоном готовим PDF для инлайн-просмотра (кэш .preview.pdf);
    # оригинал .docx остаётся доступным для скачивания
    documents.schedule_pdf_conversion(background, (row.mime_type or "").lower(), row.storage_path)
    await session.refresh(order)
    return _order_out(order, manager)


@router.delete("/{order_id}/files/{file_id}", response_model=OrderOut, summary="Удалить файл черновика")
async def delete_file(
    order_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep, manager: ManagerDep
) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    await service.delete_file(session, manager, order, file_id)
    await session.refresh(order)
    return _order_out(order, manager)


def _resolve_file(order: Order, file_id: uuid.UUID | None):
    if file_id is None:
        f = order.current_file
        if f is None:
            raise not_found("Документ приказа")
        return f
    f = next((x for x in order.files if x.id == file_id), None)
    if f is None:
        raise not_found("Файл")
    return f


async def _file_response(order: Order, f, inline: bool) -> FileResponse:
    # preview_info_async: конвертация DOCX уходит в поток и не блокирует event loop
    info = await documents.preview_info_async(f.mime_type, f.storage_path) if inline else None
    path = _storage_mod.storage.abspath(info["path"] if info else f.storage_path)
    mime = info["mime"] if info else f.mime_type
    disposition = "inline" if (inline and info and info["preview"] == "inline") else "attachment"
    filename = f.original_name
    if disposition == "inline" and mime == documents.PDF_MIME and not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return FileResponse(
        path=str(path),
        media_type=mime,
        filename=filename,
        content_disposition_type=disposition,
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
            "X-Order-Number": quote(f"{order.number}/{order.order_year}"),
        },
    )


@router.get("/{order_id}/file", summary="Скачать/посмотреть документ (?file_id=&inline=true)")
async def get_file(
    order_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    file_id: uuid.UUID | None = None,
    inline: bool = True,
) -> FileResponse:
    order = await service.get_order(session, order_id, actor)
    f = _resolve_file(order, file_id)
    return await _file_response(order, f, inline)


@router.get("/{order_id}/preview", summary="Метаданные превью документа")
async def preview_meta(order_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> dict:
    order = await service.get_order(session, order_id, actor)
    f = order.current_file or _resolve_file(order, None)
    info = await documents.preview_info_async(f.mime_type, f.storage_path)
    return {
        "order_id": str(order.id),
        "file_id": str(f.id),
        "original_name": f.original_name,
        "mime_type": f.mime_type,
        "preview": info["preview"],           # inline (PDF) | download (DOCX без конвертера)
        "url": f"/orders/{order.id}/file?file_id={f.id}&inline=true",
        "converter": "libreoffice" if info.get("converted") else None,
    }


# ------------------------------------------------------------- ознакомление
@router.post("/{order_id}/acknowledgements", response_model=AcknowledgementOut, summary="Подтвердить «Ознакомлен»")
async def acknowledge_order(
    order_id: uuid.UUID,
    payload: AckCreateIn | None,
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
) -> AcknowledgementOut:
    order = await service.get_order(session, order_id, actor)
    meta = client_meta(request)
    ack = await service.acknowledge(
        session,
        actor,
        order,
        comment=payload.comment if payload else None,
        ip=meta.get("ip"),
        user_agent=meta.get("user_agent"),
        code=payload.code if payload else None,
        word=payload.word if payload else None,
    )
    return AcknowledgementOut.model_validate(ack)


@router.get("/{order_id}/acknowledgements", response_model=list[AcknowledgementOut], summary="Лист ознакомления")
async def list_acknowledgements(
    order_id: uuid.UUID,
    session: SessionDep,
    manager: ManagerDep,
    status: AckStatus | None = None,
) -> list[AcknowledgementOut]:
    order = await service.get_order(session, order_id, manager)
    rows = order.acknowledgements
    if status is not None:
        rows = service.ack_of_status(order, status)
    rows = sorted(rows, key=lambda a: (enum_value(a.status), a.user.full_name))
    return [AcknowledgementOut.model_validate(a) for a in rows]


@router.put("/{order_id}/recipients", response_model=OrderOut, summary="Изменить круг ознакомления")
async def set_recipients(
    order_id: uuid.UUID, payload: AckListIn, session: SessionDep, manager: ManagerDep
) -> OrderOut:
    order = await service.get_order(session, order_id, manager)
    order = await service.set_recipients(session, manager, order, payload.user_ids)
    await session.flush()
    await session.refresh(order)
    await _reload_acks(session, order)
    return _order_out(order, manager)


@router.get("/{order_id}/ack-summary", summary="Сводка по ознакомлению")
async def ack_summary(order_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> dict:
    order = await service.get_order(session, order_id, manager)
    pending = service.ack_of_status(order, AckStatus.PENDING)
    return {
        "order_id": str(order.id),
        "total": order.ack_total,
        "acknowledged": order.ack_done,
        "pending": len(pending),
        "progress": round(order.ack_done / order.ack_total * 100, 1) if order.ack_total else 0.0,
        "ack_deadline": order.ack_deadline.isoformat() if order.ack_deadline else None,
        # raw-словарь минует Pydantic-сериализаторы, поэтому маскируем явно
        "pending_users": [
            {
                "id": str(a.user_id),
                "full_name": lockdown.disp(a.user.full_name, a.user_id),
                "email": "" if lockdown.masking() else a.user.email,
            }
            for a in pending
        ],
    }


@router.get("/export/ack-sheet.csv", summary="Выгрузка листа ознакомления в CSV")
async def export_ack_sheet(
    session: SessionDep,
    manager: ManagerDep,
    status: OrderStatus = OrderStatus.PUBLISHED,
) -> FileResponse:
    """Выгрузка для отчётности (CSV с BOM и разделителем «;» — открывается в Excel/LibreOffice).

    Запрос делается явным JOIN'ом по нужным колонкам: в ответе FileResponse стримится
    вне greenlet-контекста запроса, поэтому ленивая подгрузка связей здесь недопустима.
    """
    import tempfile

    rows = (
        await session.execute(
            select(
                Order.number,
                Order.order_year,
                Order.title,
                User.full_name,
                User.id,
                User.email,
                OrderAcknowledgement.status,
                OrderAcknowledgement.acknowledged_at,
                OrderAcknowledgement.sign_method,
                OrderAcknowledgement.ip_address,
                OrderAcknowledgement.comment,
            )
            .select_from(OrderAcknowledgement)
            .join(Order, Order.id == OrderAcknowledgement.order_id)
            .join(User, User.id == OrderAcknowledgement.user_id)
            .where(Order.status == status)
            .order_by(Order.issued_on.desc(), Order.number, User.full_name)
        )
    ).all()

    # должности для CSV подтягиваем отдельным запросом (связь selectin в стриминге недоступна)
    from app.models.position import Position, UserPosition

    uids = [r[4] for r in rows]
    pos_map: dict = {}
    if uids:
        pr = (
            await session.execute(
                select(UserPosition.user_id, Position.title)
                .join(Position, Position.id == UserPosition.position_id)
                .where(UserPosition.user_id.in_(uids))
                .order_by(Position.sort_order)
            )
        ).all()
        for uid, title in pr:
            pos_map.setdefault(uid, [])
            pos_map[uid].append(title)

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8-sig", newline="", prefix="ack_sheet_"
    )
    writer = safe_writer(tmp, delimiter=";")
    # ПЭП: колонки «Способ подписи» и время — подпись ознакомления в печатной форме/выгрузке
    sign_ru = {"NONE": "Без подписи (аудит)", "CLICK": "Клик «Ознакомлен»",
               "TOTP": "Код аутентификатора", "CODE_WORD": "Код + слово"}
    writer.writerow(
        ["Приказ", "Год", "Наименование", "Сотрудник", "Должность", "E-mail", "Статус",
         "Дата ознакомления", "Способ подписи", "IP", "Комментарий"]
    )
    masked = lockdown.masking()
    for number, year, title, full_name, uid, email, ack_status, ack_at, sign_method, ip, comment in rows:
        writer.writerow(
            [
                number,
                year,
                title,
                # лист ознакомления — персональные данные: в локдауне обезличиваем,
                # включая e-mail и IP (метаданные подтверждения тоже раскрывают человека)
                lockdown.disp(full_name, uid),
                "" if masked else ", ".join(pos_map.get(uid, [])),
                "" if masked else email,
                "Ознакомлен" if enum_value(ack_status) == AckStatus.ACKNOWLEDGED.value else "Не ознакомлен",
                ack_at.strftime("%d.%m.%Y %H:%M") if ack_at else "",
                sign_ru.get(str(enum_value(sign_method) if sign_method is not None else ""), ""),
                "" if masked else (ip or ""),
                comment or "",
            ]
        )
    tmp.close()

    return FileResponse(
        tmp.name,
        media_type="text/csv; charset=utf-8",
        filename=f"ack_sheet_{date.today():%Y-%m-%d}.csv",
        content_disposition_type="attachment",
        background=BackgroundTask(lambda path=tmp.name: Path(path).unlink(missing_ok=True)),
    )


