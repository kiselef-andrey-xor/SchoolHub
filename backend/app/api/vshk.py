"""API модуля ВШК (внутришкольный контроль).

Доступ:
  * чтение опубликованного годового плана — все аутентифицированные;
  * черновик, создание/импорт/публикация/редактирование/статусы/результаты — OWNER/ADMIN.
"""
from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.enums import PlanStatus, Role, VshkStatus
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.user import User
from app.models.vshk import MONTHS_RU, VshkItem, VshkPlan, academic_year_start
from app.schemas.vshk import (
    VshkImportOut,
    VshkItemIn,
    VshkItemOut,
    VshkItemUpdate,
    VshkPlanCreateIn,
    VshkPlanListOut,
    VshkPlanOut,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import audit, notifications
from app.services import storage as _storage_mod
from app.services.positions import load_directory, ref_titles, resolve_refs
from app.services.vshk_parse import parse_vshk_docx

router = APIRouter(prefix="/vshk", tags=["vshk"])
ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]

VSHK_PUBLISHED = "VSHK_PUBLISHED"
RESULT_MIMES = [
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
]


# ---------------------------------------------------------------- serializers
async def _item_out(session, item: VshkItem) -> VshkItemOut:
    out = VshkItemOut.model_validate(item)
    out.resp_titles = await ref_titles(session, item.resp or [])
    return out


async def _plan_out(session, plan: VshkPlan, actor: User) -> VshkPlanOut:
    items = sorted(plan.items or [], key=lambda i: (i.sort_order, i.id))
    out = VshkPlanOut.model_validate(plan)
    out.can_manage = actor.role in (Role.OWNER, Role.ADMIN)
    out.items = [await _item_out(session, i) for i in items]
    return out


async def _get_plan(session, plan_id: uuid.UUID, actor: User) -> VshkPlan:
    plan = (
        await session.execute(
            select(VshkPlan).options(selectinload(VshkPlan.items)).where(VshkPlan.id == plan_id)
        )
    ).scalars().first()
    if plan is None:
        raise not_found("План ВШК не найден")
    if plan.status == PlanStatus.DRAFT and actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Черновик плана ВШК виден только администрации")
    return plan


async def _get_item(session, item_id: uuid.UUID) -> VshkItem:
    item = (await session.execute(select(VshkItem).where(VshkItem.id == item_id))).scalars().first()
    if item is None:
        raise not_found("Мероприятие ВШК не найдено")
    return item


# --------------------------------------------------------------------- планы
@router.get("", response_model=list[VshkPlanListOut], summary="Список годовых планов ВШК")
async def list_plans(session: SessionDep, actor: CurrentUser) -> list[VshkPlanListOut]:
    is_staff = actor.role in (Role.OWNER, Role.ADMIN)
    stmt = select(VshkPlan, func.count(VshkItem.id)).outerjoin(
        VshkItem, VshkItem.plan_id == VshkPlan.id
    ).group_by(VshkPlan.id).order_by(VshkPlan.year_start.desc())
    if not is_staff:
        stmt = stmt.where(VshkPlan.status == PlanStatus.PUBLISHED.value)
    rows = (await session.execute(stmt)).all()
    return [
        VshkPlanListOut(
            id=p.id, year_start=p.year_start, year_label=p.year_label, status=p.status,
            items_total=n or 0, published_at=p.published_at,
        )
        for p, n in rows
    ]


@router.post("", response_model=VshkPlanOut, status_code=201, summary="Создать пустой план ВШК (OWNER/ADMIN)")
async def create_plan(payload: VshkPlanCreateIn, session: SessionDep, actor: ManagerDep) -> VshkPlanOut:
    year_start = int(payload.year_start or academic_year_start(date.today()))
    exists = (
        await session.execute(select(VshkPlan).where(VshkPlan.year_start == year_start))
    ).scalars().first()
    if exists is not None:
        raise conflict(f"План ВШК на {year_start}-{year_start + 1} учебный год уже существует")
    plan = VshkPlan(
        year_start=year_start,
        year_label=f"{year_start}-{year_start + 1}",
        status=PlanStatus.DRAFT,
        created_by_id=actor.id,
        items=[],
    )
    session.add(plan)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="VSHK_CREATE", entity_type="vshk", entity_id=plan.id,
        changes={"year": plan.year_label},
    )
    return await _plan_out(session, plan, actor)


@router.get("/{plan_id}", response_model=VshkPlanOut, summary="План ВШК целиком (мероприятия по месяцам)")
async def get_plan(plan_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> VshkPlanOut:
    plan = await _get_plan(session, plan_id, actor)
    return await _plan_out(session, plan, actor)


@router.post(
    "/import",
    response_model=VshkImportOut,
    summary="Импорт годового плана ВШК из DOCX (OWNER/ADMIN)",
)
async def import_plan(
    session: SessionDep,
    actor: ManagerDep,
    file: Annotated[UploadFile, File(description="DOCX плана ВШК: таблица «Цель | Вид | Ответственный | Срок и форма»")],
    year_start: Annotated[int | None, Form(description="Год начала учебного года; по умолчанию — текущий")] = None,
) -> VshkImportOut:
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise bad_request("Файл больше 25 МБ")
    name = (file.filename or "").lower()
    if not name.endswith(".docx") and "wordprocessingml" not in (file.content_type or ""):
        raise bad_request("Для импорта нужен файл .docx")
    if not data.startswith(b"PK"):
        raise bad_request("Файл не является DOCX (ZIP-контейнером)")

    year = year_start or academic_year_start(date.today())
    directory = await load_directory(session)
    try:
        parsed = parse_vshk_docx(data, directory)
    except ValueError as exc:
        raise bad_request(str(exc))

    plan = (
        await session.execute(
            select(VshkPlan).options(selectinload(VshkPlan.items)).where(VshkPlan.year_start == year)
        )
    ).scalars().first()
    if plan is None:
        plan = VshkPlan(
            year_start=year, year_label=f"{year}-{year + 1}",
            status=PlanStatus.DRAFT, created_by_id=actor.id, items=[],
        )
        session.add(plan)
        await session.flush()
    if plan.status != PlanStatus.DRAFT:
        raise conflict("Импортировать можно только в черновик плана — опубликованный план правится вручную")
    # повторный импорт в черновик: заменяем мероприятия
    for old in list(plan.items or []):
        await session.delete(old)
    await session.flush()
    for raw in parsed["items"]:
        session.add(VshkItem(plan_id=plan.id, **raw, status=VshkStatus.PLANNED))
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="VSHK_IMPORT", entity_type="vshk", entity_id=plan.id,
        changes={"year": plan.year_label, "items": len(parsed["items"])},
    )
    sections = {(i["month"], i["section"]) for i in parsed["items"]}
    return VshkImportOut(
        plan_id=plan.id, year_label=plan.year_label,
        items_created=len(parsed["items"]), sections=len(sections),
        warnings=parsed["warnings"],
    )


@router.post("/{plan_id}/publish", response_model=VshkPlanOut, summary="Опубликовать план ВШК (уведомляет ответственных)")
async def publish_plan(plan_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> VshkPlanOut:
    plan = await _get_plan(session, plan_id, actor)
    if plan.status != PlanStatus.DRAFT:
        raise conflict("Опубликовать можно только черновик плана ВШК")
    if not plan.items:
        raise bad_request("План пуст — добавьте хотя бы одно мероприятие")
    plan.status = PlanStatus.PUBLISHED
    plan.published_at = datetime.now(timezone.utc)
    await session.flush()

    refs: list[str] = []
    for item in plan.items:
        refs += [r for r in (item.resp or []) if r not in refs]
    resolved = await resolve_refs(session, refs)
    notified = await notifications.push_many(
        session,
        user_ids=[u.id for u in resolved if u.id != actor.id],
        type_=VSHK_PUBLISHED,
        title=f"Опубликован план ВШК на {plan.year_label} учебный год",
        body="Мероприятия, где вы ответственный, доступны в разделе «ВШК» и на дашборде",
        entity_type="vshk",
        entity_id=plan.id,
    )
    await audit.write_audit(
        session, actor=actor, action="VSHK_PUBLISH", entity_type="vshk", entity_id=plan.id,
        changes={"year": plan.year_label, "items": len(plan.items), "notified": notified},
    )
    return await _plan_out(session, plan, actor)


# ---------------------------------------------------------------- мероприятия
@router.post("/{plan_id}/items", response_model=VshkItemOut, status_code=201,
             summary="Добавить мероприятие ВШК (OWNER/ADMIN)")
async def add_item(plan_id: uuid.UUID, payload: VshkItemIn, session: SessionDep, actor: ManagerDep) -> VshkItemOut:
    plan = await _get_plan(session, plan_id, actor)
    last = max((i.sort_order for i in (plan.items or [])), default=0)
    item = VshkItem(
        plan_id=plan.id,  # plan.items загружен жадно (selectinload в _get_plan)
        month=payload.month, section=payload.section, title=payload.title,
        control_type=payload.control_type, resp=payload.resp, summary=payload.summary,
        status=payload.status, sort_order=last + 1,
    )
    session.add(item)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="VSHK_ITEM_CREATE", entity_type="vshk_item", entity_id=item.id,
        changes={"month": MONTHS_RU.get(item.month, item.month), "title": item.title[:120]},
    )
    return await _item_out(session, item)


@router.patch("/items/{item_id}", response_model=VshkItemOut, summary="Изменить мероприятие/статус ВШК (OWNER/ADMIN)")
async def update_item(item_id: uuid.UUID, payload: VshkItemUpdate, session: SessionDep, actor: ManagerDep) -> VshkItemOut:
    item = await _get_item(session, item_id)
    plan = (await session.execute(select(VshkPlan).where(VshkPlan.id == item.plan_id))).scalars().first()
    if plan is not None and plan.status == PlanStatus.ARCHIVED:
        raise conflict("План в архиве — редактирование недоступно")
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise bad_request("Пустой запрос на обновление")
    before = {"title": item.title[:120], "status": item.status.value if hasattr(item.status, "value") else item.status,
              "month": item.month}
    for field in ("month", "section", "title", "control_type", "resp", "summary", "status"):
        if field in patch and patch[field] is not None:
            setattr(item, field, patch[field])
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="VSHK_ITEM_UPDATE", entity_type="vshk_item", entity_id=item.id,
        changes={"before": before, "after": {"title": item.title[:120],
                  "status": item.status.value if hasattr(item.status, "value") else item.status,
                  "month": item.month}},
    )
    return await _item_out(session, item)


@router.delete("/items/{item_id}", status_code=204, summary="Удалить мероприятие ВШК (OWNER/ADMIN)")
async def delete_item(item_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> None:
    item = await _get_item(session, item_id)
    title = item.title[:120]
    file_path = item.result_file
    await session.delete(item)
    await session.flush()
    if file_path:
        try:
            _storage_mod.storage.delete(file_path)
        except OSError:  # pragma: no cover
            pass
    await audit.write_audit(
        session, actor=actor, action="VSHK_ITEM_DELETE", entity_type="vshk_item",
        entity_id=item_id, changes={"title": title},
    )


# ------------------------------------------------------------------ результаты
@router.post("/items/{item_id}/result", response_model=VshkItemOut,
             summary="Результат контроля: текст и/или файл справки (OWNER/ADMIN)")
async def set_result(
    item_id: uuid.UUID,
    session: SessionDep,
    actor: ManagerDep,
    result_text: Annotated[str | None, Form(max_length=2000)] = None,
    file: Annotated[UploadFile | None, File(description="PDF/DOCX/изображение справки")] = None,
) -> VshkItemOut:
    item = await _get_item(session, item_id)
    if result_text is not None:
        item.result_text = result_text.strip() or None
    if file is not None:
        data = await file.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise bad_request("Файл больше 25 МБ")
        stored = await _storage_mod.storage.save(io.BytesIO(data), file.filename or "result.pdf", file.content_type or "", RESULT_MIMES)
        old = item.result_file
        item.result_file = stored.storage_path
        item.result_file_name = stored.original_name
        if old and old != stored.storage_path:
            try:
                _storage_mod.storage.delete(old)
            except OSError:  # pragma: no cover
                pass
    item.result_at = datetime.now(timezone.utc)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="VSHK_RESULT", entity_type="vshk_item", entity_id=item.id,
        changes={"title": item.title[:120], "file": item.result_file_name},
    )
    return await _item_out(session, item)


@router.delete("/items/{item_id}/result", status_code=204, summary="Снять результат контроля (OWNER/ADMIN)")
async def clear_result(item_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> None:
    item = await _get_item(session, item_id)
    old = item.result_file
    item.result_text = None
    item.result_file = None
    item.result_file_name = None
    item.result_at = None
    await session.flush()
    if old:
        try:
            _storage_mod.storage.delete(old)
        except OSError:  # pragma: no cover
            pass
    await audit.write_audit(
        session, actor=actor, action="VSHK_RESULT_CLEAR", entity_type="vshk_item", entity_id=item.id,
    )


@router.get("/items/{item_id}/file", summary="Скачать файл результата контроля")
async def result_file(item_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> FileResponse:
    item = await _get_item(session, item_id)
    if not item.result_file:
        raise not_found("Файл результата не загружен")
    plan = (await session.execute(select(VshkPlan).where(VshkPlan.id == item.plan_id))).scalars().first()
    if plan is not None and plan.status == PlanStatus.DRAFT and actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Черновик плана ВШК виден только администрации")
    return FileResponse(
        _storage_mod.storage.abspath(item.result_file),
        media_type="application/octet-stream",
        filename=item.result_file_name or "result",
    )
