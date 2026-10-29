"""Сервис модуля «План работы на неделю».

Жизненный цикл: Черновик → Опубликован → Архив (как у приказов).
  * создают/редактируют/публикуют — OWNER/ADMIN (plans:manage);
  * видят опубликованные и архивные — все сотрудники (plans:read);
  * публикация уведомляет всех ответственных (PLAN_PUBLISHED);
  * один план на неделю: week_start (понедельник) уникален.

Content хранится JSON-документом; ссылки на сотрудников (resp, duty.teacher_id)
валидируются при сохранении: пользователь должен существовать и быть активным.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import PlanStatus, Role
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.notification import Notification
from app.models.plan import Plan
from app.models.user import User
from app.schemas.plan import PlanContentIn, PlanIn, PlanUpdateIn
from app.services import audit, notifications
from app.types import enum_value

WEEKDAY_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_manager(actor: User) -> bool:
    return actor.role in (Role.OWNER, Role.ADMIN)


def ensure_manager(actor: User) -> None:
    if not is_manager(actor):
        raise forbidden("Планы работы создаёт и редактирует администрация")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _content_to_json(content: PlanContentIn) -> dict[str, Any]:
    """Pydantic → JSON-документ (UUID ответственными строками)."""
    data = content.model_dump(mode="json")
    return data


def _iter_resp_refs(content: dict[str, Any]) -> Iterator[str]:
    for key in ("month_items", "week_items"):
        for item in content.get(key) or []:
            for ref in item.get("resp") or []:
                yield str(ref)
    for day in content.get("days") or []:
        for ev in day.get("events") or []:
            for ref in ev.get("resp") or []:
                yield str(ref)


def _all_resp_refs(content: dict[str, Any]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """(UUID сотрудников, UUID должностей) из ссылок ответственных плана."""
    user_ids: list[uuid.UUID] = []
    pos_ids: list[uuid.UUID] = []
    duty = content.get("duty") or {}
    if duty.get("teacher_id"):
        user_ids.append(uuid.UUID(str(duty["teacher_id"])))
    for ref in _iter_resp_refs(content):
        if ref == "admin" or ref.startswith("text:"):
            continue
        if ref.startswith("pos:"):
            pos_ids.append(uuid.UUID(ref[4:]))
        else:
            user_ids.append(uuid.UUID(ref))
    return user_ids, pos_ids


async def _validate_users(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, User]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    rows = await session.execute(select(User).where(User.id.in_(unique)))
    found = {u.id: u for u in rows.scalars().all()}
    for uid in unique:
        u = found.get(uid)
        if u is None or not u.is_active:
            raise bad_request("Ответственный не найден или заблокирован")
    return found


async def _validate_refs(session: AsyncSession, content: dict[str, Any]) -> None:
    """Проверка ссылок ответственных: сотрудники активны, должности существуют."""
    from app.models.position import Position

    user_ids, pos_ids = _all_resp_refs(content)
    await _validate_users(session, user_ids)
    unique_pos = list(dict.fromkeys(pos_ids))
    if not unique_pos:
        return
    rows = (await session.execute(select(Position.id).where(Position.id.in_(unique_pos)))).scalars().all()
    if len(set(rows)) != len(unique_pos):
        raise bad_request("Должность ответственного не найдена в справочнике")


# ------------------------------------------------------------------- запросы
def _base_select():
    return select(Plan).options(selectinload(Plan.created_by))


def can_see(actor: User, plan: Plan) -> bool:
    if is_manager(actor):
        return True
    return enum_value(plan.status) != PlanStatus.DRAFT.value


async def list_plans(
    session: AsyncSession,
    actor: User,
    *,
    status: str | None = None,
    year: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[Sequence[Plan], int]:
    conds = []
    if not is_manager(actor):
        conds.append(Plan.status != PlanStatus.DRAFT.value)
    if status:
        try:
            conds.append(Plan.status == PlanStatus(status.upper()))
        except ValueError:
            raise bad_request(f"Неизвестный статус плана: {status}")
    if year:
        conds.append(func.extract("year", Plan.week_start) == year)

    stmt = _base_select()
    count_stmt = select(func.count()).select_from(Plan)
    if conds:
        stmt = stmt.where(*conds)
        count_stmt = count_stmt.where(*conds)
    total = int((await session.execute(count_stmt)).scalar_one())
    stmt = stmt.order_by(Plan.week_start.desc()).limit(per_page).offset((page - 1) * per_page)
    rows = (await session.execute(stmt)).scalars().all()
    return rows, total


async def get_plan(session: AsyncSession, plan_id: uuid.UUID, actor: User) -> Plan:
    plan = (await session.execute(_base_select().where(Plan.id == plan_id))).scalar_one_or_none()
    if plan is None:
        raise not_found("План работы")
    if not can_see(actor, plan):
        raise forbidden("Черновики планов видны только администрации")
    return plan


async def current_plan(session: AsyncSession, actor: User) -> Plan | None:
    """План текущей недели (опубликованный), иначе — последний опубликованный."""
    today = date.today()
    stmt = _base_select().where(
        Plan.status == PlanStatus.PUBLISHED,
        Plan.week_start <= today,
        Plan.week_end >= today,
    ).order_by(Plan.week_start.desc()).limit(1)
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is not None:
        return plan
    stmt = _base_select().where(Plan.status == PlanStatus.PUBLISHED).order_by(Plan.week_start.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------- CRUD
async def create_plan(session: AsyncSession, actor: User, data: PlanIn) -> Plan:
    ensure_manager(actor)
    week_start = monday_of(data.week_start)
    week_end = Plan.compute_week_end(week_start)

    exists = (
        await session.execute(select(func.count()).select_from(Plan).where(Plan.week_start == week_start))
    ).scalar_one()
    if exists:
        raise conflict(f"План на неделю с {week_start:%d.%m.%Y} уже существует")

    content = _content_to_json(data.content)
    await _validate_refs(session, content)
    _check_days_in_week(content, week_start, week_end)

    plan = Plan(
        week_start=week_start,
        week_end=week_end,
        status=PlanStatus.DRAFT,
        content=content,
        created_by_id=actor.id,
    )
    session.add(plan)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="PLAN_CREATE", entity_type="plan", entity_id=plan.id,
        changes={"week": f"{week_start:%d.%m.%Y} – {week_end:%d.%m.%Y}"},
    )
    return plan


def _check_days_in_week(content: dict[str, Any], week_start: date, week_end: date) -> None:
    for day in content.get("days") or []:
        d = day.get("date")
        d = date.fromisoformat(str(d)) if not isinstance(d, date) else d
        if not (week_start <= d <= week_end):
            raise bad_request(f"Дата мероприятия {d:%d.%m.%Y} вне недели плана")


async def update_plan(session: AsyncSession, actor: User, plan: Plan, data: PlanUpdateIn) -> Plan:
    ensure_manager(actor)
    if enum_value(plan.status) == PlanStatus.ARCHIVED.value:
        raise conflict("План в архиве — редактирование недоступно")

    if data.week_start is not None and data.week_start != plan.week_start:
        week_start = monday_of(data.week_start)
        dup = (
            await session.execute(
                select(func.count()).select_from(Plan).where(
                    Plan.week_start == week_start, Plan.id != plan.id
                )
            )
        ).scalar_one()
        if dup:
            raise conflict(f"План на неделю с {week_start:%d.%m.%Y} уже существует")
        plan.week_start = week_start
        plan.week_end = Plan.compute_week_end(week_start)

    if data.content is not None:
        content = _content_to_json(data.content)
        await _validate_refs(session, content)
        _check_days_in_week(content, plan.week_start, plan.week_end)
        plan.content = content

    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="PLAN_UPDATE", entity_type="plan", entity_id=plan.id,
        changes={
            "week": f"{plan.week_start:%d.%m.%Y} – {plan.week_end:%d.%m.%Y}",
            "events": plan.events_total(),
            "status": enum_value(plan.status),
        },
    )
    return plan


async def publish_plan(session: AsyncSession, actor: User, plan: Plan) -> Plan:
    ensure_manager(actor)
    if enum_value(plan.status) != PlanStatus.DRAFT.value:
        raise conflict("Опубликовать можно только черновик плана")
    if not plan.events_total() and not (plan.content or {}).get("month_items") and not (plan.content or {}).get("week_items"):
        raise bad_request("План пуст — добавьте хотя бы одно мероприятие или пункт")

    plan.status = PlanStatus.PUBLISHED
    plan.published_at = utcnow()
    await session.flush()

    from app.services import positions as positions_service

    resolved = await positions_service.resolve_refs(session, plan.resp_refs())
    responsible = [u.id for u in resolved]
    notified = await notifications.notify_plan_published(session, plan, responsible, actor)
    await audit.write_audit(
        session, actor=actor, action="PLAN_PUBLISH", entity_type="plan", entity_id=plan.id,
        changes={
            "week": f"{plan.week_start:%d.%m.%Y} – {plan.week_end:%d.%m.%Y}",
            "events": plan.events_total(),
            "responsibles": len(responsible),
            "notified": notified,
        },
    )
    return plan


async def archive_plan(session: AsyncSession, actor: User, plan: Plan) -> Plan:
    ensure_manager(actor)
    if enum_value(plan.status) != PlanStatus.PUBLISHED.value:
        raise conflict("Архивировать можно только опубликованный план")
    plan.status = PlanStatus.ARCHIVED
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="PLAN_ARCHIVE", entity_type="plan", entity_id=plan.id,
        changes={"week": f"{plan.week_start:%d.%m.%Y} – {plan.week_end:%d.%m.%Y}"},
    )
    return plan


async def delete_plan(session: AsyncSession, actor: User, plan: Plan) -> None:
    """Черновик плана удаляет ADMIN/OWNER; архивный план — только OWNER (безвозвратно)."""
    ensure_manager(actor)
    status = enum_value(plan.status)
    if status == PlanStatus.ARCHIVED.value:
        if actor.role != Role.OWNER:
            raise forbidden("Удалять архивные планы может только Владелец")
    elif status != PlanStatus.DRAFT.value:
        raise conflict("Удалить можно только черновик плана")
    week = f"{plan.week_start:%d.%m.%Y}"
    await session.delete(plan)
    await session.execute(
        delete(Notification).where(Notification.entity_type == "plan", Notification.entity_id == plan.id)
    )
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="PLAN_DELETE", entity_type="plan", entity_id=plan.id,
        changes={"week": week, "status": status},
    )


# ------------------------------------------------------------- сериализация
def weekday_ru(d: date) -> str:
    return WEEKDAY_RU[d.weekday()]


def resolve_users(session_users: dict[uuid.UUID, User], ids: Sequence[Any]) -> list[User]:
    out: list[User] = []
    for x in ids or []:
        u = session_users.get(uuid.UUID(str(x)))
        if u is not None:
            out.append(u)
    return out


async def load_resp_context(session: AsyncSession, plan: Plan):
    """Справочники для сериализации ссылок: ({uuid: User}, {uuid: Position})."""
    from app.models.position import Position

    user_ids: list[uuid.UUID] = []
    pos_ids: list[uuid.UUID] = []
    for ref in plan.resp_refs():
        if ref.startswith("pos:"):
            try:
                pos_ids.append(uuid.UUID(ref[4:]))
            except ValueError:
                continue
        elif ref not in ("admin",) and not ref.startswith("text:"):
            try:
                user_ids.append(uuid.UUID(ref))
            except ValueError:
                continue
    users: dict[uuid.UUID, User] = {}
    if user_ids:
        rows = await session.execute(select(User).where(User.id.in_(user_ids)))
        users = {u.id: u for u in rows.scalars().all()}
    positions: dict[uuid.UUID, Position] = {}
    if pos_ids:
        rows = await session.execute(select(Position).where(Position.id.in_(pos_ids)))
        positions = {p.id: p for p in rows.scalars().all()}
    return users, positions
