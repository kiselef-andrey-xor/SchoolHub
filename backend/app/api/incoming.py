"""Журнал входящих писем.

Доступ: только Владелец и сотрудники, которым Владелец выдал грант
(`users.incoming_journal_access` — галочка в карточке пользователя).
Записи создаются автоматически из задач с «входящим номером» и вручную.
"""
from __future__ import annotations

import io
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.csvsafe import safe_writer
from app.dbutils import ilike
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.incoming import IncomingLetter
from app.models.user import User
from app.schemas.common import Page
from app.schemas.incoming import IncomingCreateIn, IncomingOut, IncomingUpdateIn
from app.security import CurrentUser, SessionDep
from app.services import audit, lockdown

router = APIRouter(prefix="/incoming", tags=["incoming"])


async def _require_access(actor: CurrentUser) -> User:
    if actor.role.value == "OWNER" or actor.incoming_journal_access:
        return actor
    raise forbidden("Доступ к журналу входящих писем выдаёт Владелец в карточке сотрудника")


AccessDep = Annotated[User, Depends(_require_access)]


async def _next_reg(session, year: int) -> int:
    max_no = (
        await session.execute(select(func.max(IncomingLetter.reg_no)).where(IncomingLetter.reg_year == year))
    ).scalar_one()
    return (max_no or 0) + 1


@router.get("", response_model=Page[IncomingOut], summary="Журнал входящих писем")
async def list_incoming(
    session: SessionDep,
    actor: AccessDep,
    search: str | None = Query(default=None, max_length=200),
    year: int | None = Query(default=None, ge=2000, le=2100),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> Page[IncomingOut]:
    stmt = select(IncomingLetter)
    conds = []
    if year:
        conds.append(IncomingLetter.reg_year == year)
    if search:
        conds.append(
            or_(
                ilike(IncomingLetter.incoming_number, search),
                ilike(IncomingLetter.subject, search),
                ilike(IncomingLetter.addressee, search),
                ilike(IncomingLetter.note, search),
            )
        )
    if conds:
        stmt = stmt.where(*conds)
    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(IncomingLetter.reg_year.desc(), IncomingLetter.reg_no.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
    ).scalars().all()
    return Page[IncomingOut](
        items=[IncomingOut.model_validate(r) for r in rows],
        total=total, page=page, per_page=per_page, pages=max(1, (total + per_page - 1) // per_page),
    )


@router.post("", response_model=IncomingOut, status_code=201, summary="Ручная запись журнала")
async def create_incoming(payload: IncomingCreateIn, session: SessionDep, actor: AccessDep) -> IncomingOut:
    number = payload.incoming_number.strip()
    if not number:
        raise bad_request("Укажите № входящего")
    incoming_date = payload.incoming_date or date.today()
    row = IncomingLetter(
        reg_year=incoming_date.year,
        reg_no=await _next_reg(session, incoming_date.year),
        incoming_date=incoming_date,
        incoming_number=number[:120],
        sent_date=payload.sent_date,
        addressee=(payload.addressee or "").strip() or None,
        subject=payload.subject.strip()[:500],
        note=(payload.note or "").strip() or None,
        responsible_id=payload.responsible_id,
        created_by_id=actor.id,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise conflict("№ регистрации уже присвоен параллельно созданной записи — обновите журнал и повторите")
    row = (
        await session.execute(select(IncomingLetter).where(IncomingLetter.id == row.id))
    ).scalar_one()
    await audit.write_audit(
        session, actor=actor, action="INCOMING_CREATE", entity_type="incoming", entity_id=row.id,
        changes={"reg": f"{row.reg_no}-{row.reg_year}", "incoming_number": number, "source": "manual"},
    )
    return IncomingOut.model_validate(row)


@router.patch("/{letter_id}", response_model=IncomingOut, summary="Правка записи журнала")
async def update_incoming(
    letter_id: uuid.UUID, payload: IncomingUpdateIn, session: SessionDep, actor: AccessDep
) -> IncomingOut:
    row = await session.get(IncomingLetter, letter_id)
    if row is None:
        raise not_found("Запись журнала")
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise bad_request("Пустой запрос")
    before = {
        "incoming_number": row.incoming_number, "incoming_date": str(row.incoming_date),
        "sent_date": str(row.sent_date), "addressee": row.addressee,
        "subject": row.subject, "note": row.note, "responsible_id": str(row.responsible_id),
    }
    new_date = patch.get("incoming_date", row.incoming_date) or row.incoming_date
    if new_date != row.incoming_date:
        # перенос в другой год — новый регистрационный номер внутри года
        if new_date.year != row.reg_year:
            row.reg_year = new_date.year
            row.reg_no = await _next_reg(session, new_date.year)
        row.incoming_date = new_date
    # ручной № регистрации применяется ПОСЛЕ переноса в другой год и проверяется
    # по итоговому году (иначе ручной номер молча затирался авто-номером)
    if "reg_no" in patch and patch["reg_no"] is not None:
        dup = (
            await session.execute(
                select(IncomingLetter.id).where(
                    IncomingLetter.reg_year == row.reg_year,
                    IncomingLetter.reg_no == patch["reg_no"],
                    IncomingLetter.id != row.id,
                ).limit(1)
            )
        ).first()
        if dup:
            raise conflict(f"№ регистрации {patch['reg_no']}/{row.reg_year} уже занят")
        row.reg_no = int(patch["reg_no"])
    for field in ("sent_date", "addressee", "note", "responsible_id"):
        if field in patch:
            setattr(row, field, patch[field])
    for field in ("incoming_number", "subject"):
        if field in patch and patch[field] is not None:
            setattr(row, field, str(patch[field]).strip()[: (120 if field == "incoming_number" else 500)] or None)
    await session.flush()
    after = {
        "incoming_number": row.incoming_number, "incoming_date": str(row.incoming_date),
        "sent_date": str(row.sent_date), "addressee": row.addressee,
        "subject": row.subject, "note": row.note, "responsible_id": str(row.responsible_id),
    }
    row = (
        await session.execute(select(IncomingLetter).where(IncomingLetter.id == row.id))
    ).scalar_one()
    await audit.write_audit(
        session, actor=actor, action="INCOMING_UPDATE", entity_type="incoming", entity_id=row.id,
        changes=audit.diff(before, after),
    )
    return IncomingOut.model_validate(row)


@router.delete("/{letter_id}", status_code=204, summary="Удалить запись журнала (только Владелец)")
async def delete_incoming(letter_id: uuid.UUID, session: SessionDep, actor: AccessDep) -> None:
    if actor.role.value != "OWNER":
        raise forbidden("Удалять записи журнала может только Владелец")
    row = await session.get(IncomingLetter, letter_id)
    if row is None:
        raise not_found("Запись журнала")
    reg = f"{row.reg_no}-{row.reg_year}"
    await session.delete(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="INCOMING_DELETE", entity_type="incoming", entity_id=row.id,
        changes={"reg": reg, "incoming_number": row.incoming_number},
    )


@router.get("/export.csv", summary="Экспорт журнала входящих в CSV")
async def export_incoming(
    session: SessionDep,
    actor: AccessDep,
    year: int | None = Query(default=None, ge=2000, le=2100),
) -> Response:
    stmt = select(IncomingLetter).order_by(IncomingLetter.reg_year, IncomingLetter.reg_no)
    if year:
        stmt = stmt.where(IncomingLetter.reg_year == year)
    rows = (await session.execute(stmt)).scalars().all()
    users: dict[uuid.UUID, str] = {}
    ids = [r.responsible_id for r in rows if r.responsible_id]
    if ids:
        fetched = (await session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all()
        users = {uid: name for uid, name in fetched}

    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow([
        "№ регистрации", "Дата входящего", "№ входящего", "Дата отправки",
        "Адресат", "Тема письма", "Примечание", "Ответственный",
    ])
    for r in rows:
        writer.writerow([
            f"{r.reg_no}/{r.reg_year}",
            r.incoming_date.isoformat(),
            r.incoming_number,
            r.sent_date.isoformat() if r.sent_date else "",
            r.addressee or "",
            r.subject,
            r.note or "",
            # ответственный — сотрудник: обезличиваем по локдауну (review §P1-1)
            lockdown.disp(users.get(r.responsible_id), r.responsible_id) if r.responsible_id else "",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),     # BOM — чтобы Excel открыл UTF-8 корректно
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="incoming-journal.csv"'},
    )
