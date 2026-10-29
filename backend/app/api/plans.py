"""API модуля «План работы на неделю».

Доступ:
  * чтение опубликованных/архивных планов — все аутентифицированные (plans:read);
  * создание/редактирование/публикация/архив/удаление — OWNER/ADMIN (plans:manage).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.enums import Role
from app.errors import bad_request
from app.models.plan import Plan
from app.models.user import User
from app.schemas.common import Page
from app.schemas.plan import (
    PlanContentOut,
    PlanDayOut,
    PlanDutyOut,
    PlanEventOut,
    PlanIn,
    PlanItemOut,
    PlanListOut,
    PlanOut,
    PlanUpdateIn,
    RespRefOut,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import plans as service

router = APIRouter(prefix="/plans", tags=["plans"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]


# ---------------------------------------------------------------- serializers
def _user_keys(actor: User) -> set[str]:
    from app.services.positions import user_resp_keys

    return user_resp_keys(actor)


def _uuid_or_none(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _plan_out(plan: Plan, actor: User, ctx) -> PlanOut:
    """ctx — кортеж (users, positions) из services.plans.load_resp_context."""
    content = plan.content or {}
    users, positions = ctx

    def _u(refs) -> list[RespRefOut]:
        out: list[RespRefOut] = []
        for ref in refs or []:
            ref = str(ref)
            if ref == "admin":
                out.append(RespRefOut(kind="admin", title="Администрация"))
            elif ref.startswith("pos:"):
                pos = positions.get(_uuid_or_none(ref[4:]))
                if pos is not None:
                    out.append(RespRefOut(kind="pos", id=str(pos.id), title=pos.title))
            elif ref.startswith("text:"):
                continue
            else:
                u = users.get(_uuid_or_none(ref))
                if u is not None:
                    out.append(RespRefOut(kind="user", id=str(u.id), title=u.full_name))
        return out

    duty = content.get("duty") or {}
    duty_out = PlanDutyOut(klass=duty.get("klass"), teacher=users.get(_uuid_or_none(duty.get("teacher_id"))))
    month = [PlanItemOut(text=i.get("text", ""), place=i.get("place"), resp=_u(i.get("resp")))
             for i in content.get("month_items") or []]
    week = [PlanItemOut(text=i.get("text", ""), place=i.get("place"), resp=_u(i.get("resp")))
            for i in content.get("week_items") or []]
    days = []
    for day in content.get("days") or []:
        d = date.fromisoformat(str(day.get("date")))
        days.append(PlanDayOut(
            date=d,
            weekday=service.weekday_ru(d),
            events=[PlanEventOut(title=e.get("title", ""), time=e.get("time"), place=e.get("place"),
                                 resp=_u(e.get("resp"))) for e in day.get("events") or []],
        ))
    return PlanOut(
        id=plan.id,
        week_start=plan.week_start,
        week_end=plan.week_end,
        status=plan.status,
        content=PlanContentOut(duty=duty_out, notes=content.get("notes") or [], month_items=month,
                               week_items=week, days=days),
        created_by=plan.created_by,
        published_at=plan.published_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        events_total=plan.events_total(),
        responsibles_total=len(plan.resp_refs()),
        my_items=plan.items_for_keys(_user_keys(actor)),
        can_manage=service.is_manager(actor),
    )


def _plan_list_out(plan: Plan, actor: User) -> PlanListOut:
    duty = (plan.content or {}).get("duty") or {}
    return PlanListOut(
        id=plan.id,
        week_start=plan.week_start,
        week_end=plan.week_end,
        status=plan.status,
        duty_class=duty.get("klass"),
        events_total=plan.events_total(),
        responsibles_total=len(plan.resp_refs()),
        my_items=plan.items_for_keys(_user_keys(actor)),
        published_at=plan.published_at,
        created_by=plan.created_by,
        can_manage=service.is_manager(actor),
    )


# ------------------------------------------------------------------ маршруты
@router.get("", response_model=Page[PlanListOut], summary="Список планов по неделям (учитель — без черновиков)")
async def list_plans(
    session: SessionDep,
    actor: CurrentUser,
    status: Annotated[str | None, Query(pattern="^(DRAFT|PUBLISHED|ARCHIVED)$")] = None,
    year: int | None = Query(default=None, ge=2000, le=2200),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
):
    rows, total = await service.list_plans(session, actor, status=status, year=year, page=page, per_page=per_page)
    return Page[PlanListOut](
        items=[_plan_list_out(p, actor) for p in rows],
        total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@router.get("/current", response_model=PlanOut | None, summary="План текущей недели (или последний опубликованный)")
async def get_current_plan(session: SessionDep, actor: CurrentUser) -> PlanOut | None:
    plan = await service.current_plan(session, actor)
    if plan is None:
        return None
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, actor, ctx)


@router.post("", response_model=PlanOut, status_code=201, summary="Создать план (OWNER/ADMIN)")
async def create_plan(payload: PlanIn, session: SessionDep, manager: ManagerDep) -> PlanOut:
    plan = await service.create_plan(session, manager, payload)
    await session.refresh(plan)
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, manager, ctx)


@router.post(
    "/parse-document",
    summary="Разобрать DOCX «План работы на неделю» в структуру редактора (OWNER/ADMIN)",
)
async def parse_plan_document(
    session: SessionDep,
    manager: ManagerDep,
    file: UploadFile = File(description="DOCX плана недели в школьном Word-макете"),
) -> dict:
    """Возвращает week_start + content (структура PlanContentIn) + warnings.

    Ответственные сопоставляются с сотрудниками SchoolHub по «Фамилия И.О.»;
    неузнанные имена не теряются (остаются в тексте с пометкой «Отв.: …»).
    """
    from app.services import plan_parse

    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise bad_request("Файл больше 25 МБ")
    name = (file.filename or "").lower()
    mime = (file.content_type or "").lower()
    if not name.endswith(".docx") and "wordprocessingml" not in mime:
        raise bad_request("Для разбора нужен файл .docx")
    if not data.startswith(b"PK"):
        raise bad_request("Файл не является DOCX (ZIP-контейнером)")
    from app.services.positions import load_directory

    directory = await load_directory(session)
    try:
        return plan_parse.parse_plan_docx(data, directory)
    except ValueError as exc:
        raise bad_request(str(exc))


@router.get("/{plan_id}", response_model=PlanOut, summary="План недели (документ целиком)")
async def get_plan(plan_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> PlanOut:
    plan = await service.get_plan(session, plan_id, actor)
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, actor, ctx)


@router.patch("/{plan_id}", response_model=PlanOut, summary="Изменить план (OWNER/ADMIN, не архив)")
async def update_plan(
    plan_id: uuid.UUID, payload: PlanUpdateIn, session: SessionDep, manager: ManagerDep
) -> PlanOut:
    plan = await service.get_plan(session, plan_id, manager)
    plan = await service.update_plan(session, manager, plan, payload)
    await session.refresh(plan)
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, manager, ctx)


@router.post("/{plan_id}/publish", response_model=PlanOut, summary="Опубликовать план (уведомляет ответственных)")
async def publish_plan(plan_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> PlanOut:
    plan = await service.get_plan(session, plan_id, manager)
    plan = await service.publish_plan(session, manager, plan)
    await session.refresh(plan)
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, manager, ctx)


@router.post("/{plan_id}/archive", response_model=PlanOut, summary="В архив (read-only)")
async def archive_plan(plan_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> PlanOut:
    plan = await service.get_plan(session, plan_id, manager)
    plan = await service.archive_plan(session, manager, plan)
    await session.refresh(plan)
    ctx = await service.load_resp_context(session, plan)
    return _plan_out(plan, manager, ctx)


@router.delete(
    "/{plan_id}",
    status_code=204,
    summary="Удалить черновик плана (OWNER/ADMIN) или архивный план (только OWNER)",
)
async def delete_plan(plan_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> None:
    plan = await service.get_plan(session, plan_id, manager)
    await service.delete_plan(session, manager, plan)
