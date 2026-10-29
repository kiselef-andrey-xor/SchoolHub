"""Уведомления, аудит-лог, дашборд, справочники (meta)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.engine import make_url
from starlette.background import BackgroundTask

from app import capabilities as caps
from app.config import settings
from app.enums import AckSignMethod, AckStatus, OrderStatus, PlanStatus, Role, SignaturePolicy, TaskStatus, VshkStatus
from app.errors import AppError, bad_request, conflict, forbidden, not_found
from app.models.audit import AuditLog
from app.models.incoming import IncomingLetter
from app.models.movement import MovementOrder
from app.models.notification import Notification
from app.models.order import Order, OrderAcknowledgement
from app.models.task import Task, TaskStatusHistory
from app.models.user import User
from app.models.vshk import academic_year_start
from app.schemas.audit import AuditOut
from app.schemas.common import EnumItem, Page
from app.schemas.dashboard import (
    BirthdayOut,
    DashboardOut,
    DashboardSummary,
    MonthSummaryOut,
    MonthTypeCount,
    MyPlanEvent,
    MyVshkItem,
    OrderStats,
    TaskStats,
    UserStats,
)
from app.schemas.notification import NotificationOut
from app.schemas.position import PositionCreate, PositionOut, PositionUpdate
from app.schemas.user import TotpCodeWordIn
from app.security import CurrentUser, SessionDep, require_role
from app.services import audit as audit_service
from app.services import audit_human, tickets
from app.services import orders as orders_service
from app.services import tasks as tasks_service
from app.services import totp as totp_service
from app.types import enum_value

# ------------------------------------------------------------------ notifications
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


@notifications_router.get("", response_model=Page[NotificationOut], summary="Мои уведомления")
async def list_notifications(
    session: SessionDep,
    actor: CurrentUser,
    unread_only: bool = False,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
) -> Page[NotificationOut]:
    conds = [Notification.user_id == actor.id]
    if unread_only:
        conds.append(Notification.is_read.is_(False))

    total = int(
        (await session.execute(select(func.count()).select_from(Notification).where(and_(*conds)))).scalar_one()
    )
    rows = (
        await session.execute(
            select(Notification)
            .where(and_(*conds))
            .order_by(Notification.created_at.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
    ).scalars().all()

    return Page[NotificationOut](
        items=[NotificationOut.model_validate(n) for n in rows],
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@notifications_router.get("/unread-count", summary="Счётчик непрочитанных")
async def unread_count(session: SessionDep, actor: CurrentUser) -> dict:
    count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == actor.id, Notification.is_read.is_(False))
            )
        ).scalar_one()
    )
    return {"unread": count}


@notifications_router.post("/{notification_id}/read", status_code=204, summary="Отметить прочитанным")
async def mark_read(notification_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    n = await session.get(Notification, notification_id)
    if n is None or n.user_id != actor.id:
        raise not_found("Уведомление")
    n.is_read = True
    await session.flush()


@notifications_router.delete("", status_code=204, summary="Удалить ВСЕ свои уведомления")
async def clear_notifications(session: SessionDep, actor: CurrentUser) -> None:
    await session.execute(delete(Notification).where(Notification.user_id == actor.id))
    await session.flush()


@notifications_router.delete("/{notification_id}", status_code=204, summary="Удалить своё уведомление")
async def delete_notification(notification_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    n = await session.get(Notification, notification_id)
    if n is None or n.user_id != actor.id:
        raise not_found("Уведомление")
    await session.delete(n)
    await session.flush()


@notifications_router.post("/read-all", status_code=204, summary="Отметить все прочитанными")
async def mark_all_read(session: SessionDep, actor: CurrentUser) -> None:
    await session.execute(
        update(Notification)
        .where(Notification.user_id == actor.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )


# ------------------------------------------------------------------------ audit
audit_router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(require_role(Role.ADMIN))],   # OWNER/ADMIN
)


@audit_router.get("", response_model=Page[AuditOut], summary="Журнал значимых действий")
async def list_audit(
    session: SessionDep,
    actor: CurrentUser,
    action: Annotated[str | None, Query(max_length=60)] = None,
    entity_type: Annotated[str | None, Query(max_length=30)] = None,
    actor_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> Page[AuditOut]:
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if actor_id:
        conds.append(AuditLog.actor_id == actor_id)
    if date_from:
        conds.append(AuditLog.created_at >= datetime(date_from.year, date_from.month, date_from.day))
    if date_to:
        conds.append(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min))

    stmt = select(AuditLog)
    if conds:
        stmt = stmt.where(and_(*conds))

    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(per_page).offset((page - 1) * per_page))
    ).scalars().all()

    human = await audit_human.humanize(session, rows)
    items: list[AuditOut] = []
    for r in rows:
        out = AuditOut.model_validate(r)
        hum = human.get(r.id) or {}
        out.summary = hum.get("summary", "")
        out.lines = hum.get("lines", [])
        items.append(out)

    return Page[AuditOut](
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@audit_router.get("/actions", response_model=list[str], summary="Справочник действий (для фильтра)")
async def audit_actions(session: SessionDep) -> list[str]:
    rows = (await session.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))).scalars().all()
    return list(rows)


@audit_router.delete("", status_code=204, summary="Очистить журнал событий (только Владелец, ПЭП «код + слово»)")
async def clear_audit(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    payload: TotpCodeWordIn | None = Body(default=None),
) -> None:
    """Журнал очищается полностью; факт очистки сам остаётся в журнале первой записью.

    Чувствительная операция: требуется электронная подпись максимального уровня —
    одноразовый код аутентификатора + контрольное слово. Без привязки — блокировка
    с подсказкой (без фолбэка на пароль)."""
    if actor.role != Role.OWNER:
        raise forbidden("Очищать журнал событий может только Владелец")
    await totp_service.require_code_word(actor, payload.code if payload else None, payload.word if payload else None)
    count = int((await session.execute(select(func.count()).select_from(AuditLog))).scalar_one())
    await session.execute(delete(AuditLog))
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="AUDIT_CLEAR",
        changes={"deleted_records": count}, request=request,
    )


# -------------------------------------------------------------------- dashboard
dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _my_plan_events(plan, keys: set[str]) -> list[MyPlanEvent]:
    """Пункты плана текущей недели, где пользователь — ответственный.

    keys — набор ссылок сотрудника (uuid, «pos:<uuid>» его должностей, «admin»
    для администрации): мероприятие с «Администрация» или с должностью попадает
    ко всем, кого касается. Порядок: дежурство, мероприятия по дням (по датам),
    пункты на неделю, на месяц.
    """
    from app.services.plans import weekday_ru

    content = plan.content or {}
    out: list[MyPlanEvent] = []

    def hit(refs) -> bool:
        return any(str(x) in keys for x in refs or [])

    duty = content.get("duty") or {}
    if duty.get("teacher_id") and str(duty["teacher_id"]) in keys:
        klass = duty.get("klass")
        out.append(MyPlanEvent(kind="duty", title=f"Дежурство по школе{f' — класс {klass}' if klass else ''}"))

    for day in content.get("days") or []:
        try:
            d = date.fromisoformat(str(day.get("date")))
        except (TypeError, ValueError):
            continue
        for ev in day.get("events") or []:
            if hit(ev.get("resp")):
                out.append(MyPlanEvent(
                    kind="event", date=d.isoformat(), weekday=weekday_ru(d),
                    time=ev.get("time"), place=ev.get("place"),
                    title=str(ev.get("title") or "").strip(),
                ))

    for kind, key in (("week", "week_items"), ("month", "month_items")):
        for it in content.get(key) or []:
            if hit(it.get("resp")):
                out.append(MyPlanEvent(kind=kind, place=it.get("place"), title=str(it.get("text") or "").strip()))

    return out[:30]


async def _my_vshk_items(session, actor) -> list[MyVshkItem]:
    """Мероприятия ВШК текущего месяца, где сотрудник — ответственный (по ссылке)."""
    from app.models.vshk import VshkItem, VshkPlan
    from app.services.positions import user_resp_keys

    today = date.today()
    year_start = academic_year_start(today)
    plan = (
        await session.execute(
            select(VshkPlan).where(
                VshkPlan.year_start == year_start,
                VshkPlan.status == PlanStatus.PUBLISHED.value,
            )
        )
    ).scalars().first()
    if plan is None:
        return []
    keys = user_resp_keys(actor)
    rows = (
        await session.execute(
            select(VshkItem)
            .where(VshkItem.plan_id == plan.id, VshkItem.month == today.month)
            .order_by(VshkItem.sort_order)
        )
    ).scalars().all()
    out: list[MyVshkItem] = []
    for it in rows:
        if not any(str(r) in keys for r in (it.resp or [])):
            continue
        out.append(MyVshkItem(
            id=str(it.id),
            plan_id=str(plan.id),
            month=it.month,
            section=it.section,
            title=it.title,
            control_type=it.control_type,
            summary=it.summary,
            status=enum_value(it.status),
            result_text=it.result_text,
        ))
    return out[:20]


@dashboard_router.get("", response_model=DashboardOut, summary="Сводка: по школе или только своя")
async def dashboard(
    session: SessionDep,
    actor: CurrentUser,
    scope: Annotated[str, Query(pattern="^(auto|mine|school)$")] = "auto",
) -> DashboardOut:
    """scope: auto — по роли; mine — только свои данные; school — по всей школе (staff)."""
    is_staff = actor.role in (Role.OWNER, Role.ADMIN)
    if scope == "school" and not is_staff:
        raise forbidden("Сводка по школе доступна только Владельцу и Администрации")
    staff_view = is_staff and scope != "mine"

    # --- задачи ---
    task_counts = await tasks_service.status_counts(
        session, actor, only_user=actor if (is_staff and not staff_view) else None
    )

    # --- приказы ---
    order_conds = [] if staff_view else [Order.status == OrderStatus.PUBLISHED.value]
    orders_total_stmt = select(func.count()).select_from(Order)
    if order_conds:
        orders_total_stmt = orders_total_stmt.where(and_(*order_conds))
    orders_total = int((await session.execute(orders_total_stmt)).scalar_one())
    orders_published = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.status == OrderStatus.PUBLISHED)
            )
        ).scalar_one()
    )
    orders_drafts = int(
        (await session.execute(select(func.count()).select_from(Order).where(Order.status == OrderStatus.DRAFT))).scalar_one()
    ) if staff_view else 0

    if staff_view:
        pending_ack = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OrderAcknowledgement)
                    .where(OrderAcknowledgement.status == AckStatus.PENDING)
                )
            ).scalar_one()
        )
    else:
        pending_ack = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OrderAcknowledgement)
                    .where(
                        OrderAcknowledgement.user_id == actor.id,
                        OrderAcknowledgement.status == AckStatus.PENDING,
                    )
                )
            ).scalar_one()
        )

    # --- пользователи (только staff) ---
    user_stats = None
    if staff_view:
        users_total = int((await session.execute(select(func.count()).select_from(User))).scalar_one())
        teachers = int(
            (await session.execute(select(func.count()).select_from(User).where(User.role == Role.TEACHER))).scalar_one()
        )
        admins = int(
            (
                await session.execute(
                    select(func.count()).select_from(User).where(User.role.in_([Role.ADMIN, Role.OWNER]))
                )
            ).scalar_one()
        )
        active = int(
            (await session.execute(select(func.count()).select_from(User).where(User.is_active.is_(True)))).scalar_one()
        )
        user_stats = UserStats(total=users_total, teachers=teachers, admins=admins, active=active)

    # --- списки ---
    pending_orders, _ = await orders_service.list_orders(
        session, actor, mine_pending=not staff_view, page=1, per_page=5,
        status=None if staff_view else OrderStatus.PUBLISHED,
    )
    recent_orders, _ = await orders_service.list_orders(
        session, actor, page=1, per_page=5, status=None if staff_view else OrderStatus.PUBLISHED
    )

    # Незавершённые задачи: у учителя — свои, у администрации «по школе» — общешкольные.
    # Сортировка по сроку: просроченные оказываются сверху, затем ближайшие.
    my_tasks_rows, _, _ = await tasks_service.list_tasks(
        session,
        actor,
        status="ACTIVE",
        assignee_id=None if staff_view else actor.id,
        sort="deadline",
        order="asc",
        page=1,
        per_page=6,
    )
    recent_tasks, _, _ = await tasks_service.list_tasks(
        session, actor, sort="created", order="desc", page=1, per_page=6,
        assignee_id=None if staff_view else actor.id,
    )

    # --- план работы текущей недели (виджет) ---
    from app.api.plans import _plan_out
    from app.services import plans as plans_service

    plan = await plans_service.current_plan(session, actor)
    plan_out = None
    my_plan_events: list[MyPlanEvent] = []
    if plan is not None:
        plan_ctx = await plans_service.load_resp_context(session, plan)
        plan_out = _plan_out(plan, actor, plan_ctx)
        # «мероприятия на этой неделе» — только если план покрывает сегодняшнюю
        # дату: иначе после публикации плана следующей недели виджет показывал бы
        # мероприятия прошедшей недели под заголовком «на этой неделе»
        if plan.week_start <= date.today() <= plan.week_end:
            from app.services.positions import user_resp_keys

            my_plan_events = _my_plan_events(plan, user_resp_keys(actor))

    # --- ВШК: мои мероприятия текущего месяца ---
    my_vshk = await _my_vshk_items(session, actor)

    # --- просроченные ознакомления (для администрации) ---
    overdue_acks = []
    if staff_view:
        from app.schemas.dashboard import OverdueAck

        today = date.today()
        rows = (
            await session.execute(
                select(Order, OrderAcknowledgement, User)
                .join(OrderAcknowledgement, OrderAcknowledgement.order_id == Order.id)
                .join(User, User.id == OrderAcknowledgement.user_id)
                .where(
                    Order.status == OrderStatus.PUBLISHED,
                    Order.ack_deadline.is_not(None),
                    Order.ack_deadline < today,
                    OrderAcknowledgement.status == AckStatus.PENDING,
                    User.is_active.is_(True),
                )
                .order_by(Order.ack_deadline)
                .limit(100)
            )
        ).all()
        overdue_acks = [
            OverdueAck(
                order_id=str(o.id),
                number=o.number,
                title=o.title,
                user_id=str(u.id),
                user_name=u.full_name,
                deadline=o.ack_deadline.isoformat(),
                days_overdue=(today - o.ack_deadline).days,
            )
            for o, _a, u in rows
        ]

    from app.api.orders import _order_list_out
    from app.api.tasks import _task_out

    # --- ближайшие дни рождения сотрудников (только staff: персональные данные) ---
    birthdays_out: list[BirthdayOut] = []
    if staff_view:
        from datetime import date as _date

        today = _date.today()

        def _anniversary(bd: _date, year: int) -> _date:
            try:
                return bd.replace(year=year)
            except ValueError:          # 29 февраля
                return bd.replace(year=year, day=28)

        rows = (
            await session.execute(
                select(User.id, User.full_name, User.birth_date).where(
                    User.birth_date.is_not(None),
                    User.is_deleted.is_(False),
                    User.is_active.is_(True),
                )
            )
        ).all()
        items: list[BirthdayOut] = []
        for uid, name, bd in rows:
            anniv = _anniversary(bd, today.year)
            if anniv < today:
                anniv = _anniversary(bd, today.year + 1)
            days_until = (anniv - today).days
            if days_until <= 30:
                items.append(
                    BirthdayOut(
                        user_id=uid,
                        full_name=name,
                        birth_date=bd,
                        days_until=days_until,
                        turns_age=anniv.year - bd.year,
                    )
                )
        birthdays_out = sorted(items, key=lambda b: b.days_until)[:10]

    return DashboardOut(
        birthdays=birthdays_out,
        current_plan=plan_out,
        my_plan_events=my_plan_events,
        my_vshk=my_vshk,
        summary=DashboardSummary(
            scope="global" if staff_view else "personal",
            orders=OrderStats(
                total=orders_total,
                published=orders_published,
                drafts=orders_drafts,
                pending_ack=pending_ack,
            ),
            tasks=TaskStats(
                total=task_counts["all"],
                new=task_counts[TaskStatus.NEW.value],
                in_progress=task_counts[TaskStatus.IN_PROGRESS.value],
                done=task_counts[TaskStatus.DONE.value],
                overdue=task_counts["OVERDUE"],
            ),
            users=user_stats,
        ),
        pending_acks=[_order_list_out(o, actor) for o in pending_orders],
        my_tasks=[_task_out(t, actor) for t in my_tasks_rows],
        recent_orders=[_order_list_out(o, actor) for o in recent_orders],
        recent_tasks=[_task_out(t, actor) for t in recent_tasks],
        overdue_acks=overdue_acks,
    )


@dashboard_router.get("/month-summary", response_model=MonthSummaryOut, summary="Отчёт «Итоги месяца»")
async def month_summary(
    session: SessionDep,
    actor: CurrentUser,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> MonthSummaryOut:
    """Счётчики за календарный месяц: приказы (с разбивкой по типам), задачи,
    журнал входящих, движение. Даты считаются по локальному времени сервера.
    Доступ: только Владелец и Администрация."""
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("«Итоги месяца» доступны только Владельцу и Администрации")
    year, mon = (int(x) for x in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)

    def in_month(col):
        # func.date приводит datetime к дате по часовой зоне сессии (PG) —
        # то есть к локальному календарному дню; на sqlite берётся дата как есть
        d = func.date(col)
        return and_(d >= start, d < end)

    # --- приказы: опубликованные за месяц, разбивка по типам ---
    pub_conds = [
        Order.status == OrderStatus.PUBLISHED.value,
        Order.published_at.is_not(None),
        in_month(Order.published_at),
    ]
    rows = (
        await session.execute(
            select(Order.order_type, func.count()).where(and_(*pub_conds)).group_by(Order.order_type)
        )
    ).all()
    by_type = [MonthTypeCount(type=t, count=int(c)) for t, c in rows]
    order_ids = [
        i for (i,) in await session.execute(select(Order.id).where(and_(*pub_conds)))
    ]
    with_pending = 0
    if order_ids:
        with_pending = int(
            (
                await session.execute(
                    select(func.count(func.distinct(OrderAcknowledgement.order_id))).where(
                        OrderAcknowledgement.order_id.in_(order_ids),
                        OrderAcknowledgement.status == AckStatus.PENDING.value,
                    )
                )
            ).scalar_one()
        )

    tasks_created = int(
        (await session.execute(select(func.count()).select_from(Task).where(in_month(Task.created_at)))).scalar_one()
    )
    tasks_done = int(
        (
            await session.execute(
                select(func.count(func.distinct(TaskStatusHistory.task_id))).where(
                    TaskStatusHistory.to_status == TaskStatus.DONE.value,
                    in_month(TaskStatusHistory.created_at),
                )
            )
        ).scalar_one()
    )
    tasks_overdue = int(
        (
            await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.deadline.is_not(None),
                    in_month(Task.deadline),
                    Task.status != TaskStatus.DONE.value,
                )
            )
        ).scalar_one()
    )
    incoming_count = int(
        (
            await session.execute(
                select(func.count()).select_from(IncomingLetter).where(in_month(IncomingLetter.incoming_date))
            )
        ).scalar_one()
    )
    movement_count = int(
        (
            await session.execute(
                select(func.count()).select_from(MovementOrder).where(in_month(MovementOrder.created_at))
            )
        ).scalar_one()
    )

    return MonthSummaryOut(
        month=month,
        orders_published=len(order_ids),
        orders_by_type=sorted(by_type, key=lambda x: -x.count),
        orders_ack_complete=len(order_ids) - with_pending,
        tasks_created=tasks_created,
        tasks_done=tasks_done,
        tasks_overdue=tasks_overdue,
        incoming_count=incoming_count,
        movement_count=movement_count,
    )


# ------------------------------------------------------------------------- meta
meta_router = APIRouter(prefix="/meta", tags=["meta"])

RUS_LABELS = {
    "OWNER": "Владелец",
    "ADMIN": "Администрация",
    "TEACHER": "Учитель",
    "HR": "По личному составу",
    "STUDENTS": "По обучающимся",
    "EDUCATION": "По учебной деятельности",
    "SAFETY": "Охрана труда и безопасность",
    "ADMIN_TYPE": "Административно-хозяйственный",
    "OTHER": "Прочее",
    "DRAFT": "Черновик",
    "PUBLISHED": "Опубликован",
    "ARCHIVED": "Архив",
    "PENDING": "Не ознакомлен",
    "ACKNOWLEDGED": "Ознакомлен",
    "NEW": "Новая",
    "IN_PROGRESS": "В работе",
    "DONE": "Выполнена",
    "OVERDUE": "Просрочена",
    "LOW": "Низкий",
    "NORMAL": "Обычный",
    "HIGH": "Высокий",
    "URGENT": "Срочный",
    # ПЭП: уровни политики подписи / фактического способа подписи (SignaturePolicy, AckSignMethod)
    "NONE": "Без подписи (только аудит)",
    "CLICK": "Клик «Ознакомлен»",
    "TOTP": "Код из аутентификатора",
    "CODE_WORD": "Код + контрольное слово",
}


@meta_router.get("/school", summary="Название школы (из первоначальной настройки) и версия сборки")
async def school_name(session: SessionDep) -> dict[str, str | None]:
    """Название школы нужно экрану входа, поэтому маршрут публичный.

    Но до завершения первоначальной настройки он отдаёт None: setup_guard
    открывает эндпоинт и в этом состоянии, а раскрывать данные о школе
    неаутентифицированному посетителю незачем (review/REVIEW.md §5.12).

    Версия отдаётся тем же запросом: Владелец видит её в профиле рядом с данными
    школы — «какая сборка стоит» нужно знать до обновления и при обращении
    в поддержку. Секретом она не является: то же значение отдаёт публичный /health.
    """
    from app.services import bootstrap
    from app.version import VERSION

    if not await bootstrap.is_configured(session):
        return {"school_name": None, "version": VERSION}
    state = await bootstrap.get_setup_state(session)
    return {"school_name": (state.school_name or "").strip() or None, "version": VERSION}


@meta_router.patch("/school", summary="Изменить название школы (только Владелец)")
async def update_school_name(
    payload: dict, session: SessionDep, actor: CurrentUser
) -> dict[str, str | None]:
    from app.services import bootstrap

    if actor.role != Role.OWNER:
        raise forbidden("Менять данные школы может только Владелец")
    name = (payload.get("school_name") or "").strip()[:200]
    state = await bootstrap.get_setup_state(session)
    state.school_name = name or None
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="SCHOOL_UPDATE", changes={"school_name": name}
    )
    return {"school_name": state.school_name}


@meta_router.get("/order-types", summary="Типы приказов (динамический справочник)")
async def list_order_types(session: SessionDep, actor: CurrentUser) -> list[dict]:
    from app.migrations import SEED_ORDER_TYPES
    from app.models.order_type import OrderTypeRow

    rows = (await session.execute(select(OrderTypeRow).order_by(OrderTypeRow.position, OrderTypeRow.code))).scalars().all()
    if not rows:
        # ленивый сид: базы, созданные в обход миграций (тесты, create_all)
        for i, (code, title) in enumerate(SEED_ORDER_TYPES):
            session.add(OrderTypeRow(code=code, title=title, position=i))
        await session.flush()
        rows = (await session.execute(select(OrderTypeRow).order_by(OrderTypeRow.position, OrderTypeRow.code))).scalars().all()
    return [{"code": r.code, "title": r.title} for r in rows]


@meta_router.post("/order-types", status_code=201, summary="Добавить тип приказа (ADMIN/OWNER)")
async def add_order_type(payload: dict, session: SessionDep, actor: CurrentUser) -> dict:
    from app.models.order_type import OrderTypeRow

    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Добавлять типы приказов могут Владелец и Администрация")
    title = str(payload.get("title") or "").strip()[:120]
    code = str(payload.get("code") or "").strip().upper()[:40]
    if not title:
        raise bad_request("Укажите название типа")
    if not code:
        import uuid as _uuid

        code = f"CUSTOM_{_uuid.uuid4().hex[:6].upper()}"
    existing = await session.get(OrderTypeRow, code)
    if existing is not None:
        raise AppError(409, f"Тип «{code}» уже существует", code="CONFLICT")
    max_pos = (await session.execute(select(func.max(OrderTypeRow.position)))).scalar_one() or 0
    row = OrderTypeRow(code=code, title=title, position=max_pos + 1)
    session.add(row)
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="ORDER_TYPE_ADD", changes={"code": code, "title": title}
    )
    return {"code": row.code, "title": row.title}


@meta_router.delete("/order-types/{code}", status_code=204, summary="Удалить тип приказа (ADMIN/OWNER)")
async def delete_order_type(code: str, session: SessionDep, actor: CurrentUser) -> None:
    from app.models.order import Order
    from app.models.order_type import OrderTypeRow

    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Удалять типы приказов могут Владелец и Администрация")
    row = await session.get(OrderTypeRow, code)
    if row is None:
        raise not_found("Тип приказа")
    used = (
        await session.execute(select(func.count()).select_from(Order).where(Order.order_type == code))
    ).scalar_one()
    if used:
        raise AppError(409, f"Тип используется в приказах ({used}). Сначала переведите их на другой тип", code="CONFLICT")
    await session.delete(row)
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="ORDER_TYPE_DELETE", changes={"code": code, "title": row.title}
    )


@meta_router.get("/enums", summary="Справочники для селектов (значения + подписи на русском)")
async def enums(_: CurrentUser) -> dict[str, list[EnumItem]]:
    # Требуется авторизация: это готовая карта предметной области (все внутренние
    # статусы и справочники), а нужна она только залогиненному интерфейсу
    # (review/REVIEW.md §5.12).
    def items(values, key_map=None) -> list[EnumItem]:
        out = []
        for v in values:
            key = key_map(v) if key_map else v
            out.append(EnumItem(value=v, label=RUS_LABELS.get(key, v)))
        return out

    return {
        "roles": items([r.value for r in Role]),
        "order_types": [],  # динамический справочник: GET /meta/order-types
        "order_statuses": items([s.value for s in OrderStatus]),
        "ack_statuses": items([s.value for s in AckStatus]),
        "task_statuses": items([s.value for s in TaskStatus] + ["OVERDUE"]),
        "plan_statuses": items([s.value for s in PlanStatus]),
        "vshk_statuses": items([s.value for s in VshkStatus]),
        "priorities": items(["LOW", "NORMAL", "HIGH", "URGENT"]),
        # ПЭП: политика подписи приказа и фактический способ подписи ознакомления
        "signature_policies": items([p.value for p in SignaturePolicy]),
        "sign_methods": items([m.value for m in AckSignMethod]),
    }


@meta_router.get("/permissions", summary="Матрица прав (RBAC) — документация и отладка")
async def permissions_matrix(_: CurrentUser) -> dict:
    return caps.full_matrix()


# ---------------------------------------------------------- должности (справочник)
@meta_router.get("/positions", response_model=list[PositionOut],
                 summary="Справочник должностей (для назначения и ответственных в плане/ВШК)")
async def list_positions(session: SessionDep, actor: CurrentUser) -> list[PositionOut]:
    from app.models.position import Position

    rows = (await session.execute(select(Position).order_by(Position.sort_order, Position.title))).scalars().all()
    if not rows:
        # ленивый сид: базы, созданные в обход миграций (тесты, create_all)
        from app.services.positions import SEED_POSITIONS

        for i, (title, aliases) in enumerate(SEED_POSITIONS, start=1):
            session.add(Position(title=title, aliases=aliases, sort_order=i))
        await session.flush()
        rows = (await session.execute(select(Position).order_by(Position.sort_order, Position.title))).scalars().all()
    return [PositionOut.model_validate(r) for r in rows]


@meta_router.post("/positions", response_model=PositionOut, status_code=201,
                  summary="Добавить должность (ADMIN/OWNER)")
async def add_position(payload: PositionCreate, session: SessionDep, actor: CurrentUser) -> PositionOut:
    from app.models.position import Position

    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Добавлять должности могут Владелец и Администрация")
    title = payload.title.strip()
    dup = (await session.execute(select(Position).where(func.lower(Position.title) == title.lower()))).scalars().first()
    if dup is not None:
        raise conflict("Такая должность уже есть в справочнике")
    max_pos = (await session.execute(select(func.max(Position.sort_order)))).scalar_one() or 0
    row = Position(title=title[:255], aliases=(payload.aliases or "").strip()[:1000], sort_order=max_pos + 1)
    session.add(row)
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="POSITION_CREATE", entity_type="position", entity_id=row.id,
        changes={"title": row.title},
    )
    return PositionOut.model_validate(row)


@meta_router.patch("/positions/{position_id}", response_model=PositionOut,
                   summary="Переименовать должность / синонимы (ADMIN/OWNER)")
async def update_position(
    position_id: uuid.UUID, payload: PositionUpdate, session: SessionDep, actor: CurrentUser
) -> PositionOut:
    from app.models.position import Position

    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Менять справочник должностей могут Владелец и Администрация")
    row = (await session.execute(select(Position).where(Position.id == position_id))).scalars().first()
    if row is None:
        raise not_found("Должность не найдена")
    if payload.title is not None:
        row.title = payload.title.strip()[:255]
    if payload.aliases is not None:
        row.aliases = (payload.aliases or "").strip()[:1000]
    if payload.sort_order is not None:
        row.sort_order = payload.sort_order
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="POSITION_UPDATE", entity_type="position", entity_id=row.id,
        changes={"title": row.title},
    )
    return PositionOut.model_validate(row)


@meta_router.delete("/positions/{position_id}", status_code=204,
                    summary="Удалить должность (ADMIN/OWNER; только свободную)")
async def delete_position(position_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    from app.models.position import Position, UserPosition

    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Менять справочник должностей могут Владелец и Администрация")
    row = (await session.execute(select(Position).where(Position.id == position_id))).scalars().first()
    if row is None:
        raise not_found("Должность не найдена")
    used = (
        await session.execute(select(func.count()).select_from(UserPosition).where(UserPosition.position_id == row.id))
    ).scalar_one()
    if used:
        raise conflict("Должность назначена сотрудникам — сначала снимите её в карточках пользователей")
    await session.delete(row)
    await session.flush()
    await audit_service.write_audit(
        session, actor=actor, action="POSITION_DELETE", entity_type="position", entity_id=row.id,
        changes={"title": row.title},
    )


# --------------------------------------------------------------------- health
health_router = APIRouter(tags=["system"])


@health_router.get("/health", summary="Healthcheck (дублирует корневой /health под префиксом API)")
async def health_api() -> dict:
    from app.config import settings as _settings
    from app.version import VERSION  # единый источник версии (§5.10)

    return {"status": "ok", "app": _settings.app_name, "env": _settings.app_env, "version": VERSION}


# --------------------------------------------------------------------- system
system_router = APIRouter(prefix="/system", tags=["system"])

# Цель билета -> маршрут, который его предъявляет
_EXPORT_TARGETS = {
    "db_backup": (tickets.PURPOSE_DB_BACKUP, "/system/backup"),
    "files_zip": (tickets.PURPOSE_FILES_ZIP, "/system/files-zip"),
}


class ExportTicketIn(BaseModel):
    """Подпись выгрузки данных (ПЭП «код + слово»).

    Выгрузка дампа БД и архива файлов раскрывает ВСЕ персональные данные школы,
    поэтому требует того же уровня подписи, что и восстановление из копии
    (review/REVIEW.md §P1-4). GET-запрос не может нести тело, поэтому код+слово
    отправляются сюда, а взамен выдаётся одноразовый короткоживущий билет.
    """

    target: str = Field(pattern="^(db_backup|files_zip)$", description="Что выгружаем")
    pep_code: str | None = Field(default=None, max_length=10)
    pep_word: str | None = Field(default=None, max_length=120)


class ExportTicketOut(BaseModel):
    ticket: str
    target: str
    url: str                 # готовый адрес выгрузки с билетом
    expires_in: int          # секунд; билет ОДНОРАЗОВЫЙ


@system_router.post("/export-ticket", response_model=ExportTicketOut,
                    summary="Подписать выгрузку данных (ПЭП «код + слово») и получить одноразовый билет")
async def export_ticket(
    payload: ExportTicketIn, session: SessionDep, actor: CurrentUser, request: Request
) -> ExportTicketOut:
    """Владелец вводит код аутентификатора и контрольное слово — система выдаёт билет
    на ОДНО скачивание (живёт 2 минуты, при использовании погашается).

    Билет можно передавать в query-строке: в отличие от access-токена он одноразовый
    и короткоживущий, поэтому попадание в access-лог nginx ничего не даёт.
    """
    if actor.role != Role.OWNER:
        raise forbidden("Выгружать данные системы может только Владелец")
    await totp_service.require_code_word(actor, payload.pep_code, payload.pep_word)

    purpose, path = _EXPORT_TARGETS[payload.target]
    ticket = tickets.issue(str(actor.id), purpose)
    await audit_service.write_audit(
        session, actor=actor, action="SYSTEM_EXPORT_TICKET",
        changes={"target": payload.target, "ttl": ticket.expires_in}, request=request,
    )
    return ExportTicketOut(
        ticket=ticket.token,
        target=payload.target,
        url=f"{path}?ticket={ticket.token}",
        expires_in=ticket.expires_in,
    )


@system_router.get("/backup", summary="Скачать резервную копию базы данных (pg_dump, только Владелец)")
async def download_backup(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    ticket: Annotated[str | None, Query(description="Одноразовый билет из POST /system/export-ticket")] = None,
) -> FileResponse:
    """Полный дамп PostgreSQL в формате plain SQL: пользователи, приказы, задачи,
    планы, журнал — всё содержимое БД. Восстановление — deploy/synology/restore-db.sh
    или `psql < дамп` на пустую базу. Для рутинных копий используйте планировщик DSM
    и backup-db.sh (ротация дампов), эта кнопка — для разовой копии перед изменениями.

    Требует подписи: сначала POST /system/export-ticket (target=db_backup) с кодом
    аутентификатора и контрольным словом, затем этот маршрут с полученным билетом.
    """
    import asyncio
    import os
    import re as _re
    import shutil
    import subprocess
    import tempfile
    from datetime import datetime

    if actor.role != Role.OWNER:
        raise forbidden("Выгружать резервную копию может только Владелец")
    tickets.consume(ticket, tickets.PURPOSE_DB_BACKUP, str(actor.id))
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        raise bad_request("Резервное копирование из интерфейса поддерживается только для PostgreSQL")
    if shutil.which("pg_dump") is None:
        raise AppError(
            503,
            "В образе контейнера нет pg_dump. Используйте backup-db.sh на NAS (планировщик DSM) "
            "или пересоберите образ с postgresql-client",
            code="BACKUP_UNAVAILABLE",
        )

    # pg_dump не умеет дампить сервер СТАРШЕ себя (PostgreSQL прерывает дамп с ошибкой
    # «version mismatch»). Проверяем заранее и говорим человеку понятными словами,
    # что делать, вместо простыни stderr из pg_dump.
    server_num = int((await session.execute(text("SHOW server_version_num"))).scalar_one())
    server_major = server_num // 10000
    dump_out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout or ""
    m = _re.search(r"\)\s*(\d+)", dump_out) or _re.search(r"(\d+)", dump_out)
    dump_major = int(m.group(1)) if m else 0
    if 0 < dump_major < server_major:
        raise AppError(
            503,
            f"pg_dump в образе контейнера версии {dump_major}, а сервер PostgreSQL — {server_major}. "
            "Дамп утилитой старее сервера PostgreSQL не делается. Пересоберите образ на NAS: "
            "sudo docker compose -f deploy/synology/docker-compose.yml up -d --build backend",
            code="BACKUP_VERSION_MISMATCH",
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".sql", prefix="schoolhub-backup-")
    os.close(fd)
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    dsn = f"postgresql://{url.username}@{url.host}:{url.port or 5432}/{url.database}"

    def _dump() -> int:
        try:
            proc = subprocess.run(
                ["pg_dump", "--no-owner", "--no-privileges", "--format=plain", "--file", tmp_path, dsn],
                env=env, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(503, "Дамп не завершился за 10 минут — попробуйте backup-db.sh на NAS", code="BACKUP_TIMEOUT") from exc
        if proc.returncode != 0:
            raise AppError(503, f"pg_dump завершился с ошибкой: {proc.stderr.strip()[:300]}", code="BACKUP_FAILED")
        return proc.returncode

    try:
        await asyncio.to_thread(_dump)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return FileResponse(
            tmp_path,
            media_type="application/sql",
            filename=f"schoolhub-backup-{stamp}.sql",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(os.unlink, tmp_path),   # не оставляем дамп в /tmp
        )
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@system_router.post("/restore", summary="Восстановить базу из резервной копии (только Владелец)")
async def restore_backup(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    file: Annotated[UploadFile, File(description="Резервная копия в формате plain SQL (.sql), до 512 МБ")],
    pep_code: Annotated[str | None, Form(description="Одноразовый код аутентификатора (ПЭП)")] = None,
    pep_word: Annotated[str | None, Form(description="Контрольное слово (ПЭП «код + слово»)")] = None,
) -> dict:
    """Полностью заменяет базу данных содержимым .sql-дампа.

    Копию берут кнопкой «Скачать резервную копию» или из backup-db.sh на NAS.
    Важно:
      * текущие данные удаляются БЕЗВОЗВРАТНО; дамп применяется атомарно
        (--single-transaction): при любой ошибке старая база остаётся нетронутой;
      * сессии хранятся в базе — после восстановления ВСЕ пользователи выйдут
        и войдут заново.
    """
    import asyncio
    import os
    import shutil
    import subprocess
    import tempfile

    if actor.role != Role.OWNER:
        raise forbidden("Восстанавливать базу из копии может только Владелец")
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        raise bad_request("Восстановление из интерфейса поддерживается только для PostgreSQL")
    # ПЭП максимального уровня: замена всей базы требует код + контрольное слово
    # (проверяется после проверки окружения — чтобы «не PostgreSQL» возвращал 400
    # независимо от наличия подписи)
    await totp_service.require_code_word(actor, pep_code, pep_word)
    if shutil.which("psql") is None:
        raise AppError(
            503,
            "В образе контейнера нет psql. Восстановите базу скриптом restore-db.sh на NAS "
            "или пересоберите образ с postgresql-client",
            code="RESTORE_UNAVAILABLE",
        )
    if not (file.filename or "").lower().endswith(".sql"):
        raise bad_request("Нужен файл .sql — дамп, который делает кнопка «Резервная копия» или backup-db.sh")

    fd, tmp_path = tempfile.mkstemp(suffix=".sql", prefix="schoolhub-restore-")
    combined: str | None = None
    try:
        # сохраняем копию во временный файл (ограничение размера — как у обычных загрузок, но больше)
        limit = 512 * 1024 * 1024
        total = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise bad_request("Файл резервной копии больше 512 МБ")
                out.write(chunk)
        if total == 0:
            raise bad_request("Файл резервной копии пустой")
        with open(tmp_path, "rb") as src:
            head = src.read(4096).decode("utf-8", "ignore")
        if "PostgreSQL database dump" not in head:
            raise bad_request(
                "Файл не похож на дамп PostgreSQL (plain SQL). Используйте копию, созданную "
                "кнопкой «Скачать резервную копию» или backup-db.sh"
            )

        # единый файл: очистка схемы + дамп → psql --single-transaction выполнит всё
        # ОДНОЙ транзакцией: при ошибке произойдёт откат и старая база сохранится
        fd2, combined = tempfile.mkstemp(suffix=".sql", prefix="schoolhub-restore-full-")
        with os.fdopen(fd2, "wb") as out:
            out.write(b"DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n")
            with open(tmp_path, "rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)

        # освобождаем блокировки и соединения пула, иначе DROP SCHEMA будет ждать
        # завершения транзакций самого бэкенда
        await session.commit()
        from app.database import engine

        await engine.dispose()

        env = {**os.environ, "PGPASSWORD": url.password or ""}
        dsn = f"postgresql://{url.username}@{url.host}:{url.port or 5432}/{url.database}"

        def _restore() -> None:
            try:
                proc = subprocess.run(
                    ["psql", dsn, "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", combined],
                    env=env, capture_output=True, text=True, timeout=1800,
                )
            except subprocess.TimeoutExpired as exc:
                raise AppError(503, "Восстановление не завершилось за 30 минут", code="RESTORE_TIMEOUT") from exc
            if proc.returncode != 0:
                raise AppError(
                    503,
                    "Восстановление прервано — СТАРЫЕ ДАННЫЕ СОХРАНЕНЫ (транзакция откатилась). "
                    f"Ошибка psql: {proc.stderr.strip()[:300]}",
                    code="RESTORE_FAILED",
                )

        await asyncio.to_thread(_restore)

        # пул переподключаем (таблицы пересозданы) и пишем отметку в аудит уже «новой» базы
        await engine.dispose()
        try:
            from app.database import SessionLocal

            async with SessionLocal() as s2:
                await audit_service.write_audit(
                    s2, actor=actor, action="SYSTEM_RESTORE",
                    changes={"file": file.filename}, request=request,
                )
                await s2.commit()
        except Exception:
            pass  # best-effort: в восстановленной базе этого пользователя может не быть

        return {"ok": True, "detail": "База данных восстановлена. Всем пользователям потребуется войти заново."}
    finally:
        for p in (tmp_path, combined):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# --------------------------------------------------- system: локдаун (обезличивание)
class LockdownIn(BaseModel):
    enabled: bool
    # ПЭП максимального уровня: включение/выключение локдауна — код + контрольное слово
    code: str | None = Field(default=None, max_length=10)
    word: str | None = Field(default=None, max_length=120)


@system_router.get("/lockdown", summary="Статус режима «Локдаун» и маскируется ли ответ для вас")
async def lockdown_status(session: SessionDep, actor: CurrentUser) -> dict:
    """enabled — флаг системы; masked_for_you — True, если в этом запросе данные
    обезличены (всегда False для Владельца)."""
    from app.services import lockdown

    return {"enabled": await lockdown.is_enabled(session), "masked_for_you": lockdown.masking()}


@system_router.post("/lockdown", summary="Локдаун: скрыть персональные данные от всех, кроме Владельца")
async def lockdown_set(
    payload: LockdownIn, session: SessionDep, actor: CurrentUser, request: Request
) -> dict:
    """Экстренный режим для демонстрации экрана и проверок: ФИО, телефоны, e-mail,
    логины, даты рождения и фото заменяются метками «Сотрудник N / Ученик N /
    Представитель N» во всех разделах, кроме сессии Владельца."""
    from app.models.system_flag import SystemFlag
    from app.services import lockdown, notifications

    if actor.role != Role.OWNER:
        raise forbidden("Включать и выключать локдаун может только Владелец")
    await totp_service.require_code_word(actor, payload.code, payload.word)
    row = await session.get(SystemFlag, lockdown.FLAG_KEY)
    if row is None:
        row = SystemFlag(key=lockdown.FLAG_KEY, value="")
        session.add(row)
    row.value = "1" if payload.enabled else "0"
    await session.flush()
    lockdown.invalidate_cache()
    lockdown.set_masking(False)   # сам Владелец всегда видит настоящие данные
    await audit_service.write_audit(
        session, actor=actor, action="LOCKDOWN_ON" if payload.enabled else "LOCKDOWN_OFF",
        request=request,
    )
    await notifications.push(
        session,
        user_id=actor.id,
        type_=notifications.LOCKDOWN,
        title="Локдаун: " + ("включён" if payload.enabled else "выключен"),
        body=(
            "Персональные данные скрыты от всех ролей, кроме Владельца."
            if payload.enabled
            else "Персональные данные снова видны согласно правам ролей."
        ),
    )
    return {"enabled": payload.enabled}


# --------------------------------------------------- system: файлы школы (ZIP)
@system_router.get("/files-summary", summary="Файлы школы: сводка по хранилищу (только Владелец)")
async def files_summary(session: SessionDep, actor: CurrentUser) -> dict:
    """Сколько файлов на диске и их объём, сколько ссылок из базы (order_files,
    task_files, аватары), есть ли битые ссылки (запись есть — файла нет) и файлы
    без записи в базе («сироты»). Основа для блока «Файлы школы» в профиле."""
    if actor.role != Role.OWNER:
        raise forbidden("Раздел «Файлы школы» доступен только Владельцу")
    from app.services import files_backup

    return await files_backup.summary(session)


@system_router.get("/files-zip", summary="Скачать все файлы школы (ZIP с manifest.json, только Владелец)")
async def files_zip(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    ticket: Annotated[str | None, Query(description="Одноразовый билет из POST /system/export-ticket")] = None,
) -> FileResponse:
    """Архив со ВСЕМИ файлами хранилища: документы приказов, вложения задач, аватары.
    Файлы лежат под исходными именами в папке files/ («Приказ 433.pdf», …), рядом —
    служебный manifest.json с путями хранилища и sha256: по нему архив восстанавливается
    кнопкой «Загрузить архив с файлами». Дамп БД файлы НЕ содержит — этот архив дополняет его."""
    import asyncio
    from datetime import datetime

    from app.services import files_backup

    if actor.role != Role.OWNER:
        raise forbidden("Выгружать архив файлов может только Владелец")
    tickets.consume(ticket, tickets.PURPOSE_FILES_ZIP, str(actor.id))

    refs = await files_backup.collect_references(session)
    tmp_path = await asyncio.to_thread(files_backup.build_zip, refs)

    await audit_service.write_audit(
        session, actor=actor, action="SYSTEM_FILES_ZIP",
        changes={"files": len(refs)}, request=request,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return FileResponse(
        path=tmp_path,
        filename=f"schoolhub-files-{stamp}.zip",
        media_type="application/zip",
        background=BackgroundTask(lambda: files_backup.cleanup_tmp(tmp_path)),
    )


@system_router.post("/files-restore", summary="Восстановить файлы школы из ZIP-архива (только Владелец)")
async def files_restore(
    session: SessionDep,
    actor: CurrentUser,
    request: Request,
    file: Annotated[UploadFile, File()],
    pep_code: Annotated[str | None, Form(description="Одноразовый код аутентификатора (ПЭП)")] = None,
    pep_word: Annotated[str | None, Form(description="Контрольное слово (ПЭП «код + слово»)")] = None,
) -> dict:
    """Восстановление из архива, созданного кнопкой «Скачать все файлы (ZIP)»:
    отсутствующие файлы возвращаются на свои пути (ссылки из базы оживают),
    одинаковые не трогаются, отличающиеся заменяются версией из архива.
    Архив без manifest.json отклоняется; чужие пути из манифеста (zip-slip)
    отбрасываются поштучно. Отчёт: restored / replaced / already / orphans / errors."""
    import asyncio
    import os
    import tempfile

    from app.services import files_backup, notifications

    if actor.role != Role.OWNER:
        raise forbidden("Восстанавливать файлы может только Владелец")

    # ПЭП максимального уровня проверяем ДО приёма архива: код+слово берутся из формы
    await totp_service.require_code_word(actor, pep_code, pep_word)

    # Архив пишем на диск ПОТОКОМ, а не читаем целиком в память: лимит контейнера
    # 460 МБ, а `await file.read()` загрузил бы весь архив в RAM ещё ДО проверки
    # размера — то есть защита не срабатывала и контейнер падал по OOM
    # (review/REVIEW.md §P1-3). Так же устроен соседний /system/restore.
    limit = settings.files_archive_max_mb * 1024 * 1024
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="schoolhub-files-restore-")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise bad_request(f"Архив больше {settings.files_archive_max_mb} МБ")
                out.write(chunk)
        if total == 0:
            raise bad_request("Пустой архив")
        report = await asyncio.to_thread(files_backup.restore_from_zip, tmp_path)
    finally:
        os.unlink(tmp_path)

    await audit_service.write_audit(
        session,
        actor=actor,
        action="SYSTEM_FILES_RESTORE",
        changes={
            "restored": len(report["restored"]),
            "replaced": len(report["replaced"]),
            "already": len(report["already"]),
            "orphans": len(report["orphans"]),
            "errors": len(report["errors"]),
        },
        request=request,
    )
    await notifications.push(
        session,
        user_id=actor.id,
        type_=notifications.FILES_RESTORE,
        title="Файлы школы: восстановление из архива",
        body=(
            f"восстановлено {len(report['restored'])}, заменено {len(report['replaced'])}, "
            f"уже на месте {len(report['already'])}, сирот {len(report['orphans'])}, "
            f"ошибок {len(report['errors'])}"
        ),
    )
    return report
