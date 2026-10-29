"""Учёт оборудования и инвентаря: справочник с количеством и местонахождением.

Доступ: Владелец + сотрудники с грантом (`users.equipment_access`,
галочка в карточке пользователя). Удаление — только Владелец.
"""
from __future__ import annotations

import csv
import io
import unicodedata
import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select

from app.csvsafe import clean_cell, safe_writer
from app.dbutils import ilike
from app.errors import bad_request, forbidden, not_found
from app.models.equipment import EquipmentItem, EquipmentMove
from app.models.user import User
from app.schemas.common import Page
from app.security import CurrentUser, SessionDep
from app.services import audit, lockdown

router = APIRouter(prefix="/equipment", tags=["equipment"])


async def _require_access(actor: CurrentUser) -> User:
    if actor.role.value == "OWNER" or actor.equipment_access:
        return actor
    raise forbidden("Доступ к учёту оборудования выдаёт Владелец в карточке сотрудника")


AccessDep = Annotated[User, Depends(_require_access)]


# Штрихкод печатается на этикетке, а SVG этикетки фронтенд вставляет через
# dangerouslySetInnerHTML: значение с «<», «>» или кавычкой стало бы исполняемым
# HTML (хранимый XSS на карточке позиции). Фронтенд подпись экранирует
# (src/lib/barcode.ts), а здесь — вторая линия защиты: разметку и управляющие
# символы не храним вовсе. Список ЗАПРЕЩЁННЫХ символов, а не разрешённых, выбран
# намеренно: в работающих школах в поле могли записать кириллицу или «№», и белый
# список заблокировал бы правку любой такой карточки. Сканер «острые» символы
# всё равно не выдаёт.
BARCODE_FORBIDDEN = set("<>\"'&\\")   # разметка + обратная косая
BARCODE_MAX_LEN = 128
BARCODE_HINT = (
    "Штрихкод не должен содержать кавычек, «ёлочек» < >, амперсанда, обратной косой "
    "и управляющих символов; длина — не больше 128"
)


def clean_barcode(value: str | None) -> str | None:
    """Нормализует штрихкод: снимает пробелы, пустое значение приводит к None.

    Бросает ValueError с человекочитаемой причиной — её же использует импорт CSV,
    чтобы показать ошибку в конкретной строке файла.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > BARCODE_MAX_LEN:
        raise ValueError(BARCODE_HINT)
    # Cc — управляющие символы (перевод строки, TAB, NUL…): ломают и сканер, и CSV
    if any(ch in BARCODE_FORBIDDEN or unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError(BARCODE_HINT)
    return text


class EquipmentIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    inventory_number: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    barcode: str | None = Field(default=None, max_length=128)
    quantity: int = Field(default=1, ge=0, le=1_000_000)
    location: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("barcode")
    @classmethod
    def _check_barcode(cls, value: str | None) -> str | None:
        return clean_barcode(value)


class EquipmentUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    inventory_number: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    barcode: str | None = Field(default=None, max_length=128)
    quantity: int | None = Field(default=None, ge=0, le=1_000_000)
    location: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("barcode")
    @classmethod
    def _check_barcode(cls, value: str | None) -> str | None:
        return clean_barcode(value)


class EquipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inventory_number: str | None = None
    serial_number: str | None = None
    barcode: str | None = None
    quantity: int
    location: str | None = None
    note: str | None = None
    created_at: datetime
    updated_at: datetime


@router.get("", response_model=Page[EquipmentOut], summary="Список оборудования и инвентаря")
async def list_equipment(
    session: SessionDep,
    actor: AccessDep,
    search: str | None = Query(default=None, max_length=200),
    location: str | None = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> Page[EquipmentOut]:
    stmt = select(EquipmentItem)
    conds = []
    if search:
        conds.append(
            or_(
                ilike(EquipmentItem.name, search),
                ilike(EquipmentItem.inventory_number, search),
                ilike(EquipmentItem.serial_number, search),
                ilike(EquipmentItem.barcode, search),
                ilike(EquipmentItem.location, search),
            )
        )
    if location:
        conds.append(ilike(EquipmentItem.location, location))
    if conds:
        stmt = stmt.where(*conds)
    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(EquipmentItem.name).limit(per_page).offset((page - 1) * per_page)
        )
    ).scalars().all()
    return Page[EquipmentOut](
        items=[EquipmentOut.model_validate(r) for r in rows],
        total=total, page=page, per_page=per_page, pages=max(1, (total + per_page - 1) // per_page),
    )


@router.get("/export.csv", summary="Выгрузка инвентаря в CSV")
async def export_equipment(session: SessionDep, actor: AccessDep) -> Response:
    rows = (await session.execute(select(EquipmentItem).order_by(EquipmentItem.name))).scalars().all()
    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow(["Наименование", "Инвентарный номер", "Заводской (серийный) номер", "Штрихкод", "Количество", "Местонахождение", "Примечание"])
    for r in rows:
        writer.writerow([
            r.name, r.inventory_number or "", r.serial_number or "", r.barcode or "",
            r.quantity, r.location or "", r.note or "",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="equipment.csv"'},
    )


@router.post("/import", summary="Импорт из CSV (формат как у экспорта: ; , BOM)")
async def import_equipment(
    session: SessionDep,
    actor: AccessDep,
    file: Annotated[UploadFile, File(description="CSV: Наименование;Инв. номер;Заводской №;Количество;Местонахождение;Примечание")],
) -> dict:
    """Идемпотентный импорт: строка с известным инвентарным номером обновляет позицию,
    остальные создают новые. Ошибки в строках не прерывают весь пакет."""
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise bad_request("Файл больше 5 МБ")
    text = data.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise bad_request("Файл пуст")
    first = lines[0]
    delimiter = max((";", "\t", ","), key=lambda d: first.count(d)) if max(first.count(";"), first.count("\t"), first.count(",")) else ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if rows and rows[0][0].strip().lower() in ("наименование", "name", "название"):
        rows = rows[1:]

    created = updated = 0
    failed: list[dict] = []
    for idx, raw in enumerate(rows, start=1):
        cells = [clean_cell(c) for c in raw]   # управляющие символы и защитный «'»
        if len(cells) >= 7:  # новый формат: со штрихкодом 4-й колонкой
            name, inv, serial, barcode, qty_s, location, note = (cells + [""] * 7)[:7]
        else:                # старый формат без штрихкода
            name, inv, serial, qty_s, location, note = (cells + [""] * 6)[:6]
            barcode = ""
        if not name:
            failed.append({"row": idx, "reason": "пустое наименование"})
            continue
        # штрихкод из файла проходит ту же проверку, что и ручной ввод: иначе
        # «острый» символ приедет в SVG этикетки прямо из импортированного CSV
        try:
            barcode = clean_barcode(barcode) or ""
        except ValueError as exc:
            failed.append({"row": idx, "reason": str(exc)})
            continue
        qty = 1
        if qty_s:
            try:
                qty = int(qty_s.replace(" ", ""))
                if qty < 0:
                    raise ValueError
            except ValueError:
                failed.append({"row": idx, "reason": f"некорректное количество: {qty_s!r}"})
                continue
        existing = None
        if inv:
            existing = (
                await session.execute(
                    select(EquipmentItem).where(EquipmentItem.inventory_number == inv).limit(1)
                )
            ).scalars().first()
        if existing is not None:
            existing.name = name[:255]
            existing.serial_number = serial or None
            if barcode:
                await _ensure_barcode_free(session, barcode, exclude_id=existing.id)
                existing.barcode = barcode[:128]
            existing.quantity = qty
            existing.location = location or None
            existing.note = note or None
            updated += 1
        else:
            if barcode:
                await _ensure_barcode_free(session, barcode)
            session.add(
                EquipmentItem(
                    name=name[:255],
                    inventory_number=inv or None,
                    serial_number=serial or None,
                    barcode=barcode[:128] or None,
                    quantity=qty,
                    location=location or None,
                    note=note or None,
                    created_by_id=actor.id,
                )
            )
            created += 1
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="EQUIPMENT_IMPORT",
        changes={"created": created, "updated": updated, "failed": len(failed)},
    )
    return {"created": created, "updated": updated, "failed": failed, "total": len(rows)}


async def _ensure_barcode_free(session, barcode: str | None, exclude_id: uuid.UUID | None = None) -> None:
    """Один штрихкод — одна позиция: иначе сканирование не сможет выбрать карточку."""
    if not barcode or not barcode.strip():
        return
    stmt = select(EquipmentItem.id).where(EquipmentItem.barcode == barcode.strip())
    if exclude_id is not None:
        stmt = stmt.where(EquipmentItem.id != exclude_id)
    if (await session.execute(stmt.limit(1))).scalars().first() is not None:
        raise bad_request(f"Штрихкод {barcode} уже присвоен другой позиции инвентаря")


@router.get("/scan", response_model=EquipmentOut, summary="Найти позицию по штрихкоду (сканер/камера)")
async def scan_equipment(session: SessionDep, actor: AccessDep, code: str = Query(min_length=1, max_length=128)) -> EquipmentOut:
    """Точка входа для сканирования: код с этикетки (EAN-13, Code-128 или инвентарный
    номер) -> карточка позиции. Аппаратные сканеры работают и без этого эндпоинта —
    как клавиатура в поле поиска списка."""
    code = code.strip()
    row = (
        await session.execute(
            select(EquipmentItem)
            .where(
                or_(
                    EquipmentItem.barcode == code,
                    EquipmentItem.inventory_number == code,
                    EquipmentItem.serial_number == code,
                )
            )
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        raise not_found(f"Позиция со штрихкодом {code}")
    return EquipmentOut.model_validate(row)


@router.post("", response_model=EquipmentOut, status_code=201, summary="Добавить позицию")
async def create_equipment(payload: EquipmentIn, session: SessionDep, actor: AccessDep) -> EquipmentOut:
    await _ensure_barcode_free(session, payload.barcode)
    row = EquipmentItem(
        name=payload.name.strip(),
        inventory_number=(payload.inventory_number or "").strip() or None,
        serial_number=(payload.serial_number or "").strip() or None,
        barcode=(payload.barcode or "").strip() or None,
        quantity=payload.quantity,
        location=(payload.location or "").strip() or None,
        note=(payload.note or "").strip() or None,
        created_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit.write_audit(
        session, actor=actor, action="EQUIPMENT_CREATE", entity_type="equipment", entity_id=row.id,
        changes={"name": row.name, "inventory_number": row.inventory_number, "quantity": row.quantity},
    )
    return EquipmentOut.model_validate(row)


@router.patch("/{item_id}", response_model=EquipmentOut, summary="Изменить позицию")
async def update_equipment(
    item_id: uuid.UUID, payload: EquipmentUpdateIn, session: SessionDep, actor: AccessDep
) -> EquipmentOut:
    row = await session.get(EquipmentItem, item_id)
    if row is None:
        raise not_found("Позиция инвентаря")
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise forbidden("Пустой запрос")
    if "barcode" in patch:
        await _ensure_barcode_free(session, patch["barcode"], exclude_id=row.id)
    before = {f: str(getattr(row, f)) for f in ("name", "inventory_number", "serial_number", "barcode", "quantity", "location", "note")}
    for field, value in patch.items():
        setattr(row, field, value.strip() if isinstance(value, str) else value)
    await session.flush()
    await session.refresh(row)
    after = {f: str(getattr(row, f)) for f in ("name", "inventory_number", "serial_number", "barcode", "quantity", "location", "note")}
    await audit.write_audit(
        session, actor=actor, action="EQUIPMENT_UPDATE", entity_type="equipment", entity_id=row.id,
        changes=audit.diff(before, after),
    )
    return EquipmentOut.model_validate(row)


class MoveIn(BaseModel):
    to_location: str = Field(min_length=1, max_length=255)
    move_date: date | None = None
    quantity: int = Field(default=1, ge=1, le=1_000_000)
    note: str | None = Field(default=None, max_length=1000)


class MoveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    item_name: str | None = None
    inventory_number: str | None = None
    from_location: str | None = None
    to_location: str
    quantity: int
    move_date: date
    note: str | None = None
    actor_name: str | None = None


@router.post("/{item_id}/move", response_model=MoveOut, summary="Переместить позицию (например, из 405 в 321)")
async def move_item(item_id: uuid.UUID, payload: MoveIn, session: SessionDep, actor: AccessDep) -> MoveOut:
    row = await session.get(EquipmentItem, item_id)
    if row is None:
        raise not_found("Позиция инвентаря")
    move = EquipmentMove(
        item_id=row.id,
        from_location=row.location,
        to_location=payload.to_location.strip()[:255],
        quantity=payload.quantity,
        move_date=payload.move_date or date.today(),
        note=(payload.note or "").strip() or None,
        actor_id=actor.id,
    )
    row.location = move.to_location
    session.add(move)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="EQUIPMENT_MOVE", entity_type="equipment", entity_id=row.id,
        changes={"name": row.name, "from": move.from_location, "to": move.to_location, "date": str(move.move_date)},
    )
    out = MoveOut.model_validate(move)
    out.item_name = row.name
    out.inventory_number = row.inventory_number
    out.actor_name = actor.full_name
    return out


@router.get("/moves", response_model=list[MoveOut], summary="Отчёт по перемещениям за месяц")
async def list_moves(
    session: SessionDep,
    actor: AccessDep,
    month: str = Query(description="ГГГГ-ММ, например 2026-10"),
) -> list[MoveOut]:
    import re as _re

    if not _re.match(r"^\d{4}-\d{2}$", month):
        raise bad_request("Месяц задаётся в формате ГГГГ-ММ")
    year, mon = int(month[:4]), int(month[5:])
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    rows = (
        await session.execute(
            select(EquipmentMove, EquipmentItem, User)
            .join(EquipmentItem, EquipmentItem.id == EquipmentMove.item_id)
            .outerjoin(User, User.id == EquipmentMove.actor_id)
            .where(EquipmentMove.move_date >= start, EquipmentMove.move_date < end)
            .order_by(EquipmentMove.move_date, EquipmentItem.name)
        )
    ).all()
    out = []
    for mv, item, user in rows:
        o = MoveOut.model_validate(mv)
        o.item_name = item.name
        o.inventory_number = item.inventory_number
        o.actor_name = lockdown.disp(user.full_name if user else None, mv.actor_id) or None
        out.append(o)
    return out


@router.get("/moves/export.csv", summary="Отчёт по перемещениям за месяц в CSV")
async def export_moves(
    session: SessionDep,
    actor: AccessDep,
    month: str = Query(description="ГГГГ-ММ"),
) -> Response:
    import re as _re

    if not _re.match(r"^\d{4}-\d{2}$", month):
        raise bad_request("Месяц задаётся в формате ГГГГ-ММ")
    year, mon = int(month[:4]), int(month[5:])
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    rows = (
        await session.execute(
            select(EquipmentMove, EquipmentItem, User)
            .join(EquipmentItem, EquipmentItem.id == EquipmentMove.item_id)
            .outerjoin(User, User.id == EquipmentMove.actor_id)
            .where(EquipmentMove.move_date >= start, EquipmentMove.move_date < end)
            .order_by(EquipmentMove.move_date, EquipmentItem.name)
        )
    ).all()
    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow(["Дата", "Наименование", "Инв. номер", "Откуда", "Куда", "Кол-во", "Основание/примечание", "Кто оформил"])
    for mv, item, user in rows:
        writer.writerow([
            mv.move_date.isoformat(), item.name, item.inventory_number or "",
            mv.from_location or "", mv.to_location, mv.quantity, mv.note or "",
            # «Кто оформил» — сотрудник: обезличиваем по локдауну (review §P1-1)
            lockdown.disp(user.full_name if user else None, mv.actor_id),
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="equipment-moves-{month}.csv"'},
    )


@router.delete("/{item_id}", status_code=204, summary="Удалить позицию (только Владелец)")
async def delete_equipment(item_id: uuid.UUID, session: SessionDep, actor: AccessDep) -> None:
    if actor.role.value != "OWNER":
        raise forbidden("Удалять позиции инвентаря может только Владелец")
    row = await session.get(EquipmentItem, item_id)
    if row is None:
        raise not_found("Позиция инвентаря")
    await session.delete(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="EQUIPMENT_DELETE", entity_type="equipment", entity_id=row.id,
        changes={"name": row.name, "inventory_number": row.inventory_number},
    )
