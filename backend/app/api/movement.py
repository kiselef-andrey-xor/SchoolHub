"""Раздел «Движение обучающихся»: приказы о переводе / зачислении / отчислении.

Доступ: Владелец + сотрудники с грантом (`users.movement_access`).
В приказе может быть МНОГО обучающихся (children); печатная форма — ОБЩАЯ на все
приказы (шаблон с плейсхолдерами, GET/PUT /movement/template).
"""
from __future__ import annotations

import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.csvsafe import safe_writer
from app.dbutils import ilike
from app.errors import forbidden, not_found
from app.models.movement import MovementChild, MovementOrder
from app.models.setup import SetupState
from app.models.user import User
from app.schemas.common import Page
from app.schemas.movement import (
    TYPE_LABELS,
    MovementChildIn,
    MovementChildOut,
    MovementIn,
    MovementOut,
    MovementUpdateIn,
)
from app.security import CurrentUser, SessionDep
from app.services import audit, lockdown

router = APIRouter(prefix="/movement", tags=["movement"])

DEFAULT_TEMPLATE = """{PREAMBLE}
ПРИКАЗЫВАЮ:
{BODY}
{CONTROL}"""


async def _require_access(actor: CurrentUser) -> User:
    if actor.role.value == "OWNER" or actor.movement_access:
        return actor
    raise forbidden("Доступ к разделу «Движение» выдаёт Владелец в карточке сотрудника")


AccessDep = Annotated[User, Depends(_require_access)]


async def _out(session, row: MovementOrder) -> MovementOut:
    kids = (
        await session.execute(
            select(MovementChild)
            .where(MovementChild.movement_id == row.id)
            .order_by(MovementChild.position)
        )
    ).scalars().all()
    out = MovementOut.model_validate(row)
    out.children = [
        MovementChildOut(id=k.id, student=k.student, birth_date=k.birth_date,
                         from_place=k.from_place, to_place=k.to_place)
        for k in kids
    ]
    if not out.children and row.student:
        # приказ создан до появления children: показываем ребёнка из старых полей
        out.children = [
            MovementChildOut(
                id=uuid.UUID(int=0), student=row.student, birth_date=row.birth_date,
                from_place=row.from_place, to_place=row.to_place,
            )
        ]
    return out


async def _replace_children(session, row: MovementOrder, kids: list[MovementChildIn]) -> None:
    old = (
        await session.execute(select(MovementChild).where(MovementChild.movement_id == row.id))
    ).scalars().all()
    for o in old:
        await session.delete(o)
    await session.flush()
    for i, k in enumerate(kids):
        session.add(
            MovementChild(
                movement_id=row.id,
                student=k.student.strip()[:255],
                birth_date=k.birth_date,
                from_place=(k.from_place or "").strip() or None,
                to_place=(k.to_place or "").strip() or None,
                position=i,
            )
        )
    await session.flush()


@router.get("", response_model=Page[MovementOut], summary="Приказы по движению обучающихся")
async def list_movement(
    session: SessionDep,
    actor: AccessDep,
    status: str | None = Query(default=None, pattern="^(NEW|DONE|ERROR)$"),
    order_type: str | None = Query(default=None, pattern="^(TRANSFER|ENROLLMENT|EXPULSION)$"),
    search: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> Page[MovementOut]:
    stmt = select(MovementOrder)
    conds = []
    if status:
        conds.append(MovementOrder.status == status)
    if order_type:
        conds.append(MovementOrder.order_type == order_type)
    if search:
        kid_ids = select(MovementChild.movement_id).where(ilike(MovementChild.student, search))
        conds.append(
            or_(
                MovementOrder.id.in_(kid_ids),
                ilike(MovementOrder.guardian, search),
                ilike(MovementOrder.order_number, search),
                ilike(MovementOrder.from_place, search),
                ilike(MovementOrder.to_place, search),
            )
        )
    if conds:
        stmt = stmt.where(*conds)
    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(MovementOrder.order_date.desc(), MovementOrder.created_at.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
    ).scalars().all()
    return Page[MovementOut](
        items=[await _out(session, r) for r in rows],
        total=total, page=page, per_page=per_page, pages=max(1, (total + per_page - 1) // per_page),
    )


@router.get("/template", summary="Текущий шаблон печатной формы (общий для всех приказов)")
async def get_template(session: SessionDep, actor: AccessDep) -> dict:
    state = await session.get(SetupState, SetupState.SINGLETON_ID)
    return {"template": (state.movement_template if state else None) or None, "default": DEFAULT_TEMPLATE}


class TemplateIn(BaseModel):
    template: str | None = None


@router.put("/template", summary="Сохранить общий шаблон печатной формы")
async def put_template(payload: TemplateIn, session: SessionDep, actor: AccessDep) -> dict:
    state = await session.get(SetupState, SetupState.SINGLETON_ID)
    if state is None:
        state = SetupState(id=SetupState.SINGLETON_ID)
        session.add(state)
        await session.flush()
    value = (payload.template or "").strip() or None
    state.movement_template = value
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="MOVEMENT_TEMPLATE_UPDATE",
        changes={"reset": value is None},
    )
    return {"template": value, "default": DEFAULT_TEMPLATE}


@router.post("", response_model=MovementOut, status_code=201, summary="Создать приказ по движению")
async def create_movement(payload: MovementIn, session: SessionDep, actor: AccessDep) -> MovementOut:
    first = payload.children[0]
    row = MovementOrder(
        order_type=payload.order_type,
        status=payload.status,
        guardian=(payload.guardian or "").strip() or None,
        student=first.student.strip()[:255],
        birth_date=first.birth_date,
        from_place=(payload.from_place or first.from_place or "").strip() or None,
        to_place=(payload.to_place or first.to_place or "").strip() or None,
        order_date=payload.order_date,
        order_number=payload.order_number.strip(),
        signatory=(payload.signatory or "").strip() or None,
        note=(payload.note or "").strip() or None,
        created_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await _replace_children(session, row, payload.children)
    await audit.write_audit(
        session, actor=actor, action="MOVEMENT_CREATE", entity_type="movement", entity_id=row.id,
        changes={
            "type": TYPE_LABELS[row.order_type], "number": row.order_number,
            "children": len(payload.children), "status": row.status,
        },
    )
    return await _out(session, row)


@router.patch("/{movement_id}", response_model=MovementOut, summary="Изменить приказ / статус / детей")
async def update_movement(
    movement_id: uuid.UUID, payload: MovementUpdateIn, session: SessionDep, actor: AccessDep
) -> MovementOut:
    row = await session.get(MovementOrder, movement_id)
    if row is None:
        raise not_found("Приказ по движению")
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise forbidden("Пустой запрос")
    fields = (
        "order_type", "guardian", "from_place", "to_place",
        "order_date", "order_number", "signatory", "note", "status",
    )
    before = {f: str(getattr(row, f)) for f in fields}
    merged = {f: getattr(row, f) for f in fields}
    merged.update({k: v for k, v in patch.items() if k in fields and v is not None})
    if merged["order_type"] not in TYPE_LABELS:
        raise forbidden("Неизвестный тип приказа")
    if merged["status"] not in ("NEW", "DONE", "ERROR"):
        raise forbidden("Неизвестный статус")
    kids = payload.children
    if kids is None:
        kids = (
            await session.execute(
                select(MovementChild).where(MovementChild.movement_id == row.id).order_by(MovementChild.position)
            )
        ).scalars().all()
        kids = [
            MovementChildIn(student=k.student, birth_date=k.birth_date, from_place=k.from_place, to_place=k.to_place)
            for k in kids
        ]
    if not kids:
        raise forbidden("В приказе должен остаться хотя бы один обучающийся")
    for k in kids:
        if merged["order_type"] in ("TRANSFER", "EXPULSION") and not (k.from_place or "").strip() and not (merged["from_place"] or "").strip():
            raise forbidden("Для перевода и отчисления укажите «откуда»")
        if merged["order_type"] in ("TRANSFER", "ENROLLMENT") and not (k.to_place or "").strip() and not (merged["to_place"] or "").strip():
            raise forbidden("Для перевода и зачисления укажите «куда»")
    for field in fields:
        if field in patch:
            value = patch[field]
            setattr(row, field, value.strip() if isinstance(value, str) else value)
    if payload.children is not None:
        await _replace_children(session, row, payload.children)
        # денормализация первой строки для списков/поиска
        row.student = payload.children[0].student.strip()[:255] if payload.children else row.student
        row.birth_date = payload.children[0].birth_date if payload.children else row.birth_date
    await session.flush()
    await session.refresh(row)
    after = {f: str(getattr(row, f)) for f in fields}
    await audit.write_audit(
        session, actor=actor, action="MOVEMENT_UPDATE", entity_type="movement", entity_id=row.id,
        changes=audit.diff(before, after) | ({"children": len(payload.children)} if payload.children is not None else {}),
    )
    return await _out(session, row)


@router.delete("/{movement_id}", status_code=204, summary="Удалить приказ (только Владелец)")
async def delete_movement(movement_id: uuid.UUID, session: SessionDep, actor: AccessDep) -> None:
    if actor.role.value != "OWNER":
        raise forbidden("Удалять приказы по движению может только Владелец")
    row = await session.get(MovementOrder, movement_id)
    if row is None:
        raise not_found("Приказ по движению")
    reg = row.order_number
    await session.delete(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="MOVEMENT_DELETE", entity_type="movement", entity_id=row.id,
        changes={"number": reg, "student": row.student},
    )


@router.get("/export.csv", summary="Сводка по выбранным приказам (CSV для внешней системы)")
async def export_movement(
    session: SessionDep,
    actor: AccessDep,
    ids: str | None = Query(default=None, description="Список id через запятую; пусто — все"),
) -> Response:
    stmt = select(MovementOrder).order_by(MovementOrder.order_date, MovementOrder.order_number)
    if ids:
        wanted: list[uuid.UUID] = []
        for part in ids.split(","):
            try:
                wanted.append(uuid.UUID(part.strip()))
            except ValueError:
                continue
        if wanted:
            stmt = stmt.where(MovementOrder.id.in_(wanted))
    rows = (await session.execute(stmt)).scalars().all()

    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow([
        "Тип приказа", "№ приказа", "Дата приказа", "Обучающийся", "Дата рождения",
        "Законный представитель", "Откуда", "Куда", "Статус", "Примечание",
    ])
    status_labels = {"NEW": "новый", "DONE": "выполнен", "ERROR": "ошибка"}
    for r in rows:
        kids = (
            await session.execute(
                select(MovementChild).where(MovementChild.movement_id == r.id).order_by(MovementChild.position)
            )
        ).scalars().all()
        if kids:
            for k in kids:
                writer.writerow([
                    TYPE_LABELS[r.order_type], r.order_number, r.order_date.isoformat(),
                    # обучающийся/представитель — свободный текст, не пользователи:
                    # метка строится от самого имени, как в schemas/movement.py
                    lockdown.disp(k.student, kind="student"),
                    "" if (lockdown.masking() and k.birth_date) else (k.birth_date.isoformat() if k.birth_date else ""),
                    lockdown.disp(r.guardian, kind="guardian"),
                    k.from_place or r.from_place or "", k.to_place or r.to_place or "",
                    status_labels.get(r.status, r.status), r.note or "",
                ])
        else:
            writer.writerow([
                TYPE_LABELS[r.order_type], r.order_number, r.order_date.isoformat(),
                lockdown.disp(r.student, kind="student"),
                "" if (lockdown.masking() and r.birth_date) else (r.birth_date.isoformat() if r.birth_date else ""),
                lockdown.disp(r.guardian, kind="guardian"), r.from_place or "", r.to_place or "",
                status_labels.get(r.status, r.status), r.note or "",
            ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="movement-summary.csv"'},
    )
