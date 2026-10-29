"""Сервис Task Manager (v1.3 — повторяющиеся задачи, несколько исполнителей, отметки, вложения).

Ключевые проектные решения:
  * «Просрочена» (OVERDUE) — ВЫЧИСЛЯЕМОЕ состояние: (status != DONE) AND (deadline < now).
    Хранить его нельзя (устаревает само), поэтому оно считается в SQL-предикатах
    и отдаётся полями effective_status / is_overdue / days_left.
  * Исполнителей НЕСКОЛЬКО (task_assignees). У каждого — своя отметка «выполнено»
    (done_at). Отметились все → задача автоматически закрывается (DONE);
    снятие отметки возвращает задачу в IN_PROGRESS. Ручные переходы статуса
    администрацией работают как раньше (state machine).
  * ПОВТОРЯЮЩИЕСЯ задачи (recurrence = DAILY/WEEKLY/MONTHLY): в БД одна строка,
    копии на каждый день НЕ плодятся. Выполненная в текущем периоде задача
    автоматически возвращается в NEW к началу следующего периода
    (rollover_due — из фонового цикла напоминаний и из списков/карточек задач).
    Дедлайна у таких задач нет: срок задаёт сам период, OVERDUE не наступает.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from fastapi import UploadFile
from sqlalchemy import Select, and_, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.dbutils import ilike
from app.enums import OVERDUE, TASK_TRANSITIONS, Role, TaskStatus
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.notification import Notification
from app.models.task import (
    Task,
    TaskAssignee,
    TaskComment,
    TaskFile,
    TaskOccurrence,
    TaskPriority,
    TaskRecurrence,
    TaskStatusHistory,
)
from app.models.user import User
from app.schemas.task import TaskCreateIn, TaskUpdateIn
from app.services import audit, document_pdf, notifications
from app.services import storage as _storage_mod
from app.types import enum_value


# ----------------------------------------------------------------- вычисления
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_recurring(task: Task) -> bool:
    return (task.recurrence or TaskRecurrence.NONE) != TaskRecurrence.NONE


WEEKDAY_RU = {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс"}


def recurrence_label(rec: str | None, weekdays: list[int] | None = None, day: int | None = None) -> str:
    """Человеческая подпись периодичности («каждый день», «еженедельно (пн, ср)»)."""
    rec = (rec or TaskRecurrence.NONE).upper()
    if rec == TaskRecurrence.DAILY:
        return "каждый день"
    if rec == TaskRecurrence.WEEKLY:
        days = ", ".join(WEEKDAY_RU.get(int(d), "?") for d in sorted(weekdays or []) if 1 <= int(d) <= 7)
        return f"еженедельно ({days})" if days else "еженедельно"
    if rec == TaskRecurrence.MONTHLY:
        return f"ежемесячно ({day}-го числа)" if day else "ежемесячно"
    return ""


def _clamp_day(year: int, month: int, day: int) -> date:
    """Число месяца с переносом на последний день коротких месяцев (29–31)."""
    return date(year, month, min(max(day, 1), calendar.monthrange(year, month)[1]))


def period_start_date(task: Task, today: date | None = None) -> date | None:
    """Дата начала ТЕКУЩЕГО периода повторения (локальные сутки).

    DAILY   — сегодня;
    WEEKLY  — ближайший прошедший (включая сегодня) день из recurrence_weekdays;
    MONTHLY — ближайшее прошедшее число из recurrence_day (с переносом 29–31).

    None — задача не повторяется.
    """
    rec = (task.recurrence or TaskRecurrence.NONE).upper()
    today = today or date.today()
    if rec == TaskRecurrence.DAILY:
        return today
    if rec == TaskRecurrence.WEEKLY:
        days = sorted({int(d) for d in (task.recurrence_weekdays or []) if 1 <= int(d) <= 7})
        if not days:
            return today
        if today.isoweekday() in days:
            return today
        for back in range(1, 8):
            candidate = today - timedelta(days=back)
            if candidate.isoweekday() in days:
                return candidate
        return today  # pragma: no cover — недостижимо при валидных днях недели
    if rec == TaskRecurrence.MONTHLY:
        day = int(task.recurrence_day or 1)
        anchor = _clamp_day(today.year, today.month, day)
        if today >= anchor:
            return anchor
        prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return _clamp_day(prev_year, prev_month, day)
    return None


def next_period_start(task: Task, from_start: date) -> date:
    """Начало СЛЕДУЮЩЕГО периода повторения — строго позже `from_start`.

    Идём вперёд по суткам, пока период, накрывающий очередную дату, не сменится.
    Максимальный шаг — месяц (≤31 суток); 62 — гарантированный запас.
    """
    for step in range(1, 63):
        candidate = from_start + timedelta(days=step)
        nxt = period_start_date(task, candidate)
        if nxt is not None and nxt != from_start:
            return nxt
    return from_start  # pragma: no cover — вырожденная конфигурация повторения


# ------------------------------------------------- история повторов (v1.10)
def _occurrence_insert(dialect_name: str):
    """Диалектный INSERT для task_occurrences.

    Обе продуктовые СУБД (PostgreSQL и SQLite) поддерживают ON CONFLICT —
    он страхует от гонки: два параллельных rollover_due (фоновый цикл + запрос
    пользователя) не должны падать на UNIQUE(task_id, period_start).
    """
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert
    return _insert(TaskOccurrence)


async def _sync_occurrence_summary(session: AsyncSession, task: Task) -> None:
    """Пересчитать missed_streak / last_occurrence_* из task_occurrences.

    Денормализация на задаче нужна, чтобы бейдж «провалено N» в списке задач
    не делал отдельный запрос на каждую строку. Источник истины — таблица.
    """
    await session.flush()
    rows = (
        await session.execute(
            select(TaskOccurrence)
            .where(TaskOccurrence.task_id == task.id)
            .order_by(TaskOccurrence.period_start.desc())
            .limit(400)
        )
    ).scalars().all()
    streak = 0
    for r in rows:
        if r.status == "MISSED":
            streak += 1
        else:
            break
    task.missed_streak = streak
    task.last_occurrence_start = rows[0].period_start if rows else None
    task.last_occurrence_status = rows[0].status if rows else None


async def record_occurrence_done(session: AsyncSession, task: Task, *, actor: User | None = None) -> None:
    """Записать DONE за ТЕКУЩИЙ период повторяющейся задачи (upsert).

    Вызывается при закрытии задачи: change_status(→DONE) и set_my_mark, когда
    отметились все исполнители. Для обычных (неповторяющихся) задач — no-op.
    """
    if not is_recurring(task):
        return
    start = period_start_date(task)
    if start is None:  # pragma: no cover — is_recurring гарантирует период
        return
    now = utcnow()
    by_ids = [str(a.user_id) for a in task.assignees if a.done_at is not None]
    if not by_ids and actor is not None:
        by_ids = [str(actor.id)]
    completed_at = _aware(task.completed_at) or now
    row = (
        await session.execute(
            select(TaskOccurrence).where(
                TaskOccurrence.task_id == task.id, TaskOccurrence.period_start == start
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # upsert: параллельное закрытие того же периода не должно ронять запрос
        conn = await session.connection()
        stmt = (
            _occurrence_insert(conn.dialect.name)
            .values(
                id=uuid.uuid4(), task_id=task.id, period_start=start, status="DONE",
                completed_at=completed_at, completed_by_ids=by_ids, closed_at=now,
            )
            .on_conflict_do_update(
                index_elements=["task_id", "period_start"],
                set_={
                    "status": "DONE", "completed_at": completed_at,
                    "completed_by_ids": by_ids, "closed_at": now,
                },
            )
        )
        await session.execute(stmt)
    else:
        row.status, row.completed_at = "DONE", completed_at
        row.completed_by_ids, row.closed_at = by_ids, now
    # период закрыт — курсор переводим на следующий; провалом он уже не станет
    task.occurrence_cursor = next_period_start(task, start)
    await _sync_occurrence_summary(session, task)


async def retract_current_occurrence(session: AsyncSession, task: Task) -> None:
    """Откатить запись DONE за текущий период (сняли отметку / вернули из DONE).

    Период снова считается открытым: строка удаляется, курсор возвращается на
    его начало — если период так и не выполнят, rollover закроет его как MISSED.
    """
    if not is_recurring(task):
        return
    start = period_start_date(task)
    if start is None:  # pragma: no cover
        return
    await session.execute(
        delete(TaskOccurrence).where(
            TaskOccurrence.task_id == task.id, TaskOccurrence.period_start == start
        )
    )
    task.occurrence_cursor = start
    await _sync_occurrence_summary(session, task)


def is_done_this_period(task: Task) -> bool:
    """Выполнена ли задача в ТЕКУЩЕМ периоде повторения.

    Для обычных задач — это просто status == DONE. Для повторяющихся DONE,
    поставленный в прошлых периодах, уже не считается: к началу нового периода
    задача «воскресает» (сначала — через effective_status, затем rollover_due
    физически сбрасывает статус и отметки исполнителей).
    """
    if enum_value(task.status) != TaskStatus.DONE.value:
        return False
    start = period_start_date(task)
    if start is None:
        return True
    done = _aware(task.completed_at)
    if done is None:
        return False
    return done.astimezone().date() >= start


def is_overdue(task: Task, now: datetime | None = None) -> bool:
    if is_recurring(task):
        return False  # у повторяющихся задач нет разового дедлайна — просрочки не бывает
    now = _aware(now) or utcnow()
    deadline = _aware(task.deadline)
    return bool(deadline and enum_value(task.status) != TaskStatus.DONE.value and deadline < now)


def effective_status(task: Task, now: datetime | None = None) -> str:
    if is_overdue(task, now):
        return OVERDUE
    stored = enum_value(task.status)
    if stored == TaskStatus.DONE.value and is_recurring(task) and not is_done_this_period(task):
        # период сменился, автосброс ещё не отработал — показываем задачу новой
        return TaskStatus.NEW.value
    return stored


def days_left(task: Task, now: datetime | None = None) -> int | None:
    """Сколько полных суток осталось до дедлайна (отрицательное = просрочено)."""
    if is_recurring(task):
        return None
    deadline = _aware(task.deadline)
    if deadline is None:
        return None
    now = _aware(now) or utcnow()
    return (deadline - now).days


def _status_value(task: Task) -> str:
    return enum_value(task.status)


# --------------------------------------------------- повторяющиеся: автосброс
async def rollover_due(session: AsyncSession) -> int:
    """Сброс повторяющихся задач к началу нового периода.

    Одна строка в БД на задачу: вместо ежедневного плодящихся копий задача
    возвращается в NEW, отметки исполнителей очищаются, completed_at сбрасывается.
    last_done_at сохраняется — по нему видно, когда задачу выполняли в последний раз.

    Дополнительно чистятся УСТАРЕВШИЕ отметки исполнителей у незакрытых задач:
    отметка прошлого периода не должна засчитываться в новом (иначе задача
    закрылась бы, когда отметится только часть исполнителей).

    v1.10 — история повторов: истёкшие периоды, которые так и не были выполнены,
    закрываются строкой MISSED в task_occurrences (курсор задачи occurrence_cursor
    отмечает первый незакрытый период). Выполненные периоды уже содержат DONE —
    их закрывает record_occurrence_done в момент выполнения, повторного закрытия
    не происходит. Для задач, созданных ДО включения истории (курсор NULL),
    отсчёт начинается «с сегодня»: задним числом периоды не проваливаются.

    История статусов и аудит-лог НЕ пишутся осознанно: ежедневная задача давала бы
    сотни одинаковых строк шума (для периодов теперь есть task_occurrences).

    Вызывается из фонового цикла напоминаний (ежечасно, поэтому к утру всё
    «воскресает» само) и из list_tasks/status_counts/get_task — чтобы интерфейс
    показывал актуальное состояние даже в первую минуту после полуночи.
    """
    rows = (
        await session.execute(
            select(Task)
            .options(selectinload(Task.assignees))
            .where(Task.recurrence != TaskRecurrence.NONE)
        )
    ).scalars().all()
    now = utcnow()
    touched = 0

    # --- фаза 1: у кого есть незакрытые прошлые периоды (чисто в памяти) ---
    plans: list[tuple[Task, list[date]]] = []
    task_ids: list[uuid.UUID] = []
    min_cursor: date | None = None
    for t in rows:
        start = period_start_date(t)
        if start is None:
            continue
        if t.occurrence_cursor is None:
            # история только начинается: задним числом периоды не проваливаем
            t.occurrence_cursor = start
            touched += 1
        periods: list[date] = []
        cursor = t.occurrence_cursor
        guard = 0
        while cursor < start and guard < 400:
            periods.append(cursor)
            nxt = next_period_start(t, cursor)
            if nxt <= cursor:  # pragma: no cover — защита от сломанной конфигурации
                cursor = start
                break
            cursor = nxt
            guard += 1
        if periods:
            t.occurrence_cursor = start
            touched += 1
            plans.append((t, periods))
            task_ids.append(t.id)
            if min_cursor is None or periods[0] < min_cursor:
                min_cursor = periods[0]

    # --- фаза 2: один запрос — какие из этих периодов уже закрыты (DONE) ---
    already: set[tuple[uuid.UUID, date]] = set()
    if task_ids and min_cursor is not None:
        res = await session.execute(
            select(TaskOccurrence.task_id, TaskOccurrence.period_start).where(
                TaskOccurrence.task_id.in_(task_ids),
                TaskOccurrence.period_start >= min_cursor,
            )
        )
        already = {(row[0], row[1]) for row in res.all()}

    # --- фаза 3: закрыть истёкшие периоды как MISSED, пересчитать сводку ---
    dirty: list[Task] = []
    missing: list[dict[str, Any]] = []
    for t, periods in plans:
        added = False
        for ps in periods:
            if (t.id, ps) in already:
                continue
            missing.append({
                "id": uuid.uuid4(),
                "task_id": t.id,
                "period_start": ps,
                "status": "MISSED",
                "completed_at": None,
                "completed_by_ids": None,
                "closed_at": now,
            })
            added = True
        if added:
            dirty.append(t)
    if missing:
        # один INSERT на все задачи; ON CONFLICT DO NOTHING — защита от гонки
        # двух параллельных rollover_due (фоновый цикл + запрос пользователя)
        conn = await session.connection()
        await session.execute(
            _occurrence_insert(conn.dialect.name)
            .on_conflict_do_nothing(index_elements=["task_id", "period_start"])
            .values(missing)
        )
    for t in dirty:
        await _sync_occurrence_summary(session, t)

    for t in rows:
        start = period_start_date(t)
        if start is None:
            continue
        changed = False
        if _status_value(t) == TaskStatus.DONE.value and not is_done_this_period(t):
            # задача закрыта в ПРОШЛОМ периоде — сбрасываем её вместе со всеми
            # отметками: они относятся к прошлому выполнению
            t.status = TaskStatus.NEW
            t.completed_at = None
            for a in t.assignees:
                a.done_at = None
            changed = True
        else:
            # задача не закрыта, но часть отметок могла остаться с прошлых
            # периодов — они устарели и не должны засчитываться
            for a in t.assignees:
                done = _aware(a.done_at)
                if done is not None and done.astimezone().date() < start:
                    a.done_at = None
                    changed = True
        if changed:
            touched += 1
    if touched:
        await session.flush()
    return touched


def validate_recurrence(
    recurrence: str | None, weekdays: list[int] | None, day: int | None
) -> tuple[str, list[int] | None, int | None]:
    """Валидация полей повторяемости; возвращает нормализованную тройку."""
    rec = (recurrence or TaskRecurrence.NONE).upper()
    if rec not in TaskRecurrence.ALL:
        raise bad_request("Некорректная периодичность повторения задачи")
    wd: list[int] | None = None
    dm: int | None = None
    if rec == TaskRecurrence.WEEKLY:
        wd = sorted({int(d) for d in (weekdays or []) if str(d).lstrip("-").isdigit()})
        if not wd or any(d < 1 or d > 7 for d in wd):
            raise bad_request("Для еженедельной задачи укажите дни недели (пн–вс)")
    elif rec == TaskRecurrence.MONTHLY:
        if day is None:
            raise bad_request("Для ежемесячной задачи укажите число месяца (1–31)")
        dm = int(day)
        if not 1 <= dm <= 31:
            raise bad_request("Число месяца должно быть от 1 до 31")
    return rec, wd, dm


# ------------------------------------------------------------------- доступы
def is_assignee(actor: User, task: Task) -> bool:
    return any(a.user_id == actor.id for a in task.assignees)


def can_edit(actor: User, task: Task) -> bool:
    """Кто может менять задачу.

    OWNER/ADMIN  — любую (постановщики);
    TEACHER      — только где он исполнитель, и только рабочие поля
                    (статус/описание/комментарии/отметка/вложения),
                    без переназначения, дедлайна и приоритета.
    """
    if actor.role in (Role.OWNER, Role.ADMIN):
        return True
    return is_assignee(actor, task)


def can_see(actor: User, task: Task) -> bool:
    if actor.role in (Role.OWNER, Role.ADMIN):
        return True                       # админ видит ВСЕ задачи
    return is_assignee(actor, task) or task.creator_id == actor.id


def ensure_can_edit(actor: User, task: Task) -> None:
    if not can_edit(actor, task):
        raise forbidden("Вы не можете редактировать эту задачу")


# ------------------------------------------------------------------- запросы
def _base_select() -> Select:
    return select(Task).options(
        selectinload(Task.creator),
        selectinload(Task.assignees).selectinload(TaskAssignee.user),
        selectinload(Task.files).selectinload(TaskFile.uploaded_by),
    )


def scope_filter(actor: User):
    """Row-level scoping: учитель — только свои задачи (исполнитель или постановщик)."""
    if actor.role in (Role.OWNER, Role.ADMIN):
        return None
    assigned = exists().where(and_(TaskAssignee.task_id == Task.id, TaskAssignee.user_id == actor.id))
    return or_(assigned, Task.creator_id == actor.id)


async def _resolve_assignees(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[User]:
    """Валидация списка исполнителей: существуют, активны, без дублей."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    rows = await session.execute(select(User).where(User.id.in_(unique)))
    found = {u.id: u for u in rows.scalars().all()}
    users: list[User] = []
    for uid in unique:
        user = found.get(uid)
        if user is None or not user.is_active:
            raise bad_request("Исполнитель не найден или заблокирован")
        users.append(user)
    return users


async def list_tasks(
    session: AsyncSession,
    actor: User,
    *,
    status: str | None = None,          # NEW | IN_PROGRESS | DONE | OVERDUE
    assignee_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    priority: str | None = None,
    search: str | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    sort: str = "deadline",             # deadline | created | priority | title
    order: str = "asc",
    page: int = 1,
    per_page: int = 20,
    mine_only: bool = False,            # staff: только где я исполнитель/постановщик
    recurring: bool | None = None,      # True — только повторяющиеся, False — только обычные
) -> tuple[Sequence[Task], int, dict[str, int]]:
    now = utcnow()
    # повторяющиеся задачи, выполненные в прошлых периодах, «воскресают» до выборки —
    # иначе после полуночи список показывал бы вчерашние выполненные вместо новых
    await rollover_due(session)
    conds = []

    scoped = scope_filter(actor)
    if scoped is not None:
        conds.append(scoped)
    if mine_only and actor.role in (Role.OWNER, Role.ADMIN):
        assigned = exists().where(and_(TaskAssignee.task_id == Task.id, TaskAssignee.user_id == actor.id))
        conds.append(or_(assigned, Task.creator_id == actor.id))

    if assignee_id:
        if actor.role == Role.TEACHER and assignee_id != actor.id:
            raise forbidden("Учитель может фильтровать только по себе")
        conds.append(exists().where(and_(TaskAssignee.task_id == Task.id, TaskAssignee.user_id == assignee_id)))
    if creator_id:
        conds.append(Task.creator_id == creator_id)
    if priority:
        conds.append(Task.priority == priority)
    if recurring is True:
        conds.append(Task.recurrence != TaskRecurrence.NONE)
    elif recurring is False:
        conds.append(Task.recurrence == TaskRecurrence.NONE)
    if search:
        conds.append(or_(ilike(Task.title, search), ilike(Task.description, search)))
    if due_after:
        conds.append(Task.deadline >= due_after)
    if due_before:
        conds.append(Task.deadline <= due_before)

    # статус-фильтр (OVERDUE — составной предикат; ACTIVE — всё, кроме выполненных)
    if status:
        status = status.upper()
        if status == "ACTIVE":
            conds.append(Task.status != TaskStatus.DONE)
        elif status == OVERDUE:
            conds.append(and_(Task.deadline.is_not(None), Task.deadline < now, Task.status != TaskStatus.DONE))
        elif status in {s.value for s in TaskStatus}:
            if status == TaskStatus.DONE.value:
                conds.append(Task.status == TaskStatus.DONE.value)
            else:
                conds.append(and_(Task.status == TaskStatus(status), or_(Task.deadline.is_(None), Task.deadline >= now)))
        else:
            raise bad_request(f"Неизвестный статус: {status}")

    stmt = _base_select()
    count_stmt = select(func.count()).select_from(Task)
    if conds:
        stmt = stmt.where(and_(*conds))
        count_stmt = count_stmt.where(and_(*conds))
    if sort == "reg":
        from app.models.incoming import IncomingLetter

        stmt = stmt.outerjoin(IncomingLetter, IncomingLetter.task_id == Task.id)

    total = int((await session.execute(count_stmt)).scalar_one())

    if sort == "reg":
        # сверху вниз по убыванию номера регистрации (без регистрации — в конце)
        stmt = stmt.order_by(
            IncomingLetter.reg_year.desc().nulls_last(),
            IncomingLetter.reg_no.desc().nulls_last(),
        )
    else:
        sort_map = {
            "deadline": (Task.deadline.is_(None), Task.deadline),
            "created": (Task.created_at,),
            "priority": (Task.priority,),
            "title": (Task.title,),
            "updated": (Task.updated_at,),
        }
        cols = sort_map.get(sort, sort_map["deadline"])
        stmt = stmt.order_by(*[c.desc() if order == "desc" else c.asc() for c in cols])
    stmt = stmt.limit(per_page).offset((page - 1) * per_page)

    tasks = (await session.execute(stmt)).scalars().all()
    counts = await status_counts(
        session, actor, only_user=actor if (mine_only and actor.role in (Role.OWNER, Role.ADMIN)) else None
    )
    return tasks, total, counts


async def status_counts(session: AsyncSession, actor: User, *, only_user: User | None = None) -> dict[str, int]:
    """Счётчики по статусам (включая вычисляемый OVERDUE) в пределах видимости пользователя.

    only_user — принудительно сузить до задач конкретного сотрудника (нужно для
    персональной сводки администрации: исполнитель или постановщик).
    """
    await rollover_due(session)   # счётчики должны учитывать «воскресшие» повторяющиеся
    now = utcnow()
    base = select(func.count()).select_from(Task)
    scoped = scope_filter(actor)
    if scoped is not None:
        base = base.where(scoped)
    if only_user is not None:
        assigned = exists().where(and_(TaskAssignee.task_id == Task.id, TaskAssignee.user_id == only_user.id))
        base = base.where(or_(assigned, Task.creator_id == only_user.id))

    async def count(*extra) -> int:
        stmt = base.where(and_(*extra)) if extra else base
        return int((await session.execute(stmt)).scalar_one())

    done = await count(Task.status == TaskStatus.DONE)
    overdue = await count(
        Task.deadline.is_not(None), Task.deadline < now, Task.status != TaskStatus.DONE
    )
    new = await count(Task.status == TaskStatus.NEW, or_(Task.deadline.is_(None), Task.deadline >= now))
    in_progress = await count(
        Task.status == TaskStatus.IN_PROGRESS, or_(Task.deadline.is_(None), Task.deadline >= now)
    )
    return {
        "all": done + overdue + new + in_progress,
        "ACTIVE": overdue + new + in_progress,   # всё, кроме выполненных (лента «Все задачи»)
        TaskStatus.NEW.value: new,
        TaskStatus.IN_PROGRESS.value: in_progress,
        TaskStatus.DONE.value: done,
        OVERDUE: overdue,
    }


async def get_task(session: AsyncSession, task_id: uuid.UUID, actor: User, *, with_details: bool = True) -> Task:
    stmt = _base_select()
    if with_details:
        stmt = stmt.options(
            selectinload(Task.comments).selectinload(TaskComment.author),
            selectinload(Task.history).selectinload(TaskStatusHistory.actor),
        )
    await rollover_due(session)   # карточка всегда показывает актуальный период
    task = (await session.execute(stmt.where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise not_found("Задача")
    if not can_see(actor, task):
        # 403, а не 404: факт существования задачи не скрываем от сотрудника школы
        raise forbidden("Нет доступа к этой задаче")
    return task


# --------------------------------------------------------------------- CRUD
async def create_task(session: AsyncSession, actor: User, data: TaskCreateIn) -> Task:
    if actor.role == Role.TEACHER:
        raise forbidden("Создавать задачи могут Владелец и Администрация")

    if data.priority not in TaskPriority.ALL:
        raise bad_request("Некорректный приоритет")

    assignees = await _resolve_assignees(session, data.assignee_ids)

    recurrence, rec_weekdays, rec_day = validate_recurrence(
        data.recurrence, data.recurrence_weekdays, data.recurrence_day
    )
    recurring = recurrence != TaskRecurrence.NONE

    deadline = None if recurring else _aware(data.deadline)
    if deadline and deadline < utcnow():
        raise bad_request("Дедлайн не может быть в прошлом")

    incoming_number = (data.incoming_number or "").strip() or None
    task = Task(
        title=data.title.strip(),
        description=data.description,
        status=TaskStatus.NEW,
        priority=data.priority,
        creator_id=actor.id,
        deadline=deadline,
        incoming_number=incoming_number,
        recurrence=recurrence,
        recurrence_weekdays=rec_weekdays,
        recurrence_day=rec_day,
    )
    if recurring:
        # история повторов стартует сразу: текущий период уже можно провалить
        task.occurrence_cursor = period_start_date(task)
    for user in assignees:
        task.assignees.append(TaskAssignee(user_id=user.id))
    session.add(task)
    await session.flush()
    if incoming_number:
        await incoming_sync(session, task, actor, reg_no=data.reg_no)

    session.add(
        TaskStatusHistory(task_id=task.id, from_status=None, to_status=TaskStatus.NEW.value, actor_id=actor.id,
                          note="Задача создана")
    )
    if recurring:
        log_event(session, task, actor, f"Повторение: {recurrence_label(recurrence, rec_weekdays, rec_day)}")
    if assignees:
        log_event(
            session, task, actor,
            "Назначены исполнители: " + ", ".join(u.full_name for u in assignees),
        )
    await notifications.notify_task_assigned(session, task, actor, [u.id for u in assignees])
    await audit.write_audit(
        session, actor=actor, action="TASK_CREATE", entity_type="task", entity_id=task.id,
        changes={
            "title": task.title,
            "assignees": [u.full_name for u in assignees],
            "deadline": str(task.deadline),
            **({"recurrence": recurrence_label(recurrence, rec_weekdays, rec_day)} if recurring else {}),
        },
    )
    await session.flush()
    return task


async def update_task(session: AsyncSession, actor: User, task: Task, data: TaskUpdateIn) -> Task:
    ensure_can_edit(actor, task)
    patch: dict[str, Any] = data.model_dump(exclude_unset=True)
    if not patch:
        return task
    changed_keys = set(patch)   # помним запрошенные поля: patch ниже чистится pop()

    is_teacher = actor.role == Role.TEACHER
    if is_teacher and set(patch) - {"description", "status"}:
        raise forbidden("Учитель может менять только описание и статус своей задачи")

    before = {
        "title": task.title, "description": task.description, "priority": task.priority,
        "assignees": sorted(str(x) for x in task.assignee_ids),
        "deadline": str(task.deadline), "status": _status_value(task),
        "recurrence": recurrence_label(task.recurrence, task.recurrence_weekdays, task.recurrence_day),
    }

    if "status" in patch and patch["status"] is not None:
        await change_status(
            session, actor, task,
            patch["status"] if isinstance(patch["status"], TaskStatus) else TaskStatus(patch["status"]),
        )
    patch.pop("status", None)

    # --- повторяемость: валидируется СВЯЗКОЙ полей (новых и уже сохранённых) ---
    recurrence_touched = bool({"recurrence", "recurrence_weekdays", "recurrence_day"} & set(patch))
    if recurrence_touched:
        eff_rec = patch.get("recurrence", task.recurrence)
        eff_wd = patch.get("recurrence_weekdays", task.recurrence_weekdays)
        eff_day = patch.get("recurrence_day", task.recurrence_day)
        # смена периода на другой тип — старые параметры не переносим
        if "recurrence" in patch and patch["recurrence"] != task.recurrence:
            eff_wd = patch.get("recurrence_weekdays")
            eff_day = patch.get("recurrence_day")
        rec, wd, dm = validate_recurrence(eff_rec, eff_wd, eff_day)
        task.recurrence, task.recurrence_weekdays, task.recurrence_day = rec, wd, dm
        # курсор истории повторов переводим на новые рельсы; периоды по старому
        # календарю задним числом не проваливаем
        task.occurrence_cursor = period_start_date(task) if rec != TaskRecurrence.NONE else None
        patch.pop("recurrence", None)
        patch.pop("recurrence_weekdays", None)
        patch.pop("recurrence_day", None)

    recurring = is_recurring(task)

    if "assignee_ids" in patch:
        new_ids = [uuid.UUID(str(x)) for x in (patch.pop("assignee_ids") or [])]
        users = await _resolve_assignees(session, new_ids)
        current = {a.user_id: a for a in task.assignees}
        target = {u.id for u in users}
        added = [u for u in users if u.id not in current]
        removed_ids = [uid for uid in current if uid not in target]
        removed_names = [
            (await session.get(User, uid)).full_name if (await session.get(User, uid)) else str(uid)
            for uid in removed_ids
        ]
        # убрать отсутствующих (вместе с их отметками), добавить новых
        for uid, row in list(current.items()):
            if uid not in target:
                await session.delete(row)
                task.assignees.remove(row)
        added = [u for u in users if u.id not in current]
        for user in added:
            task.assignees.append(TaskAssignee(task_id=task.id, user_id=user.id))
        await session.flush()
        if added:
            await notifications.notify_task_assigned(session, task, actor, [u.id for u in added])

    if "deadline" in patch:
        dl = _aware(patch.pop("deadline"))
        if dl and dl < utcnow() and _status_value(task) != TaskStatus.DONE.value:
            raise bad_request("Дедлайн не может быть в прошлом")
        task.deadline = dl
    if recurring:
        # у повторяющихся задач разового дедлайна нет: срок задаёт период повторения
        task.deadline = None

    if "priority" in patch:
        pr = patch.pop("priority")
        if pr not in TaskPriority.ALL:
            raise bad_request("Некорректный приоритет")
        task.priority = pr

    if "incoming_number" in patch:
        task.incoming_number = (patch.get("incoming_number") or "").strip() or None
    manual_reg_no = patch.pop("reg_no", None)   # ручной № регистрации для журнала входящих

    for field in ("title", "description"):
        if field in patch and patch[field] is not None:
            setattr(task, field, str(patch[field]).strip() if field == "title" else patch[field])

    # построчные события истории по изменённым полям
    if "assignee_ids" in changed_keys:
        if added:
            log_event(session, task, actor, "Назначены исполнители: " + ", ".join(u.full_name for u in added))
        if removed_names:
            log_event(session, task, actor, "Сняты исполнители: " + ", ".join(removed_names))
    if recurrence_touched and before["recurrence"] != recurrence_label(
        task.recurrence, task.recurrence_weekdays, task.recurrence_day
    ):
        log_event(
            session, task, actor,
            "Повторение: «{}» → «{}»".format(
                before["recurrence"] or "не повторять",
                recurrence_label(task.recurrence, task.recurrence_weekdays, task.recurrence_day) or "не повторять",
            ),
        )
    for field, label in (
        ("title", "Название"), ("deadline", "Срок"), ("priority", "Приоритет"),
        ("description", "Описание"), ("incoming_number", "Входящий №"),
    ):
        if field in changed_keys and before.get(field) != after_value(task, field):
            log_event(
                session, task, actor,
                f"{label}: «{fmt_field(before.get(field))}» → «{fmt_field(after_value(task, field))}»",
            )

    await session.flush()

    after = {
        "title": task.title, "description": task.description, "priority": task.priority,
        "assignees": sorted(str(x) for x in task.assignee_ids),
        "deadline": str(task.deadline), "status": _status_value(task),
        "recurrence": recurrence_label(task.recurrence, task.recurrence_weekdays, task.recurrence_day),
    }
    changes = audit.diff(before, after)
    if changes:
        await audit.write_audit(
            session, actor=actor, action="TASK_UPDATE", entity_type="task", entity_id=task.id, changes=changes
        )
        notify_ids = {a.user_id for a in task.assignees} | ({task.creator_id} if task.creator_id else set())
        if changes.keys() & {"deadline", "title", "description", "priority", "recurrence"}:
            await notifications.push_many(
                session,
                user_ids=[uid for uid in notify_ids if uid and uid != actor.id],
                type_=notifications.TASK_STATUS_CHANGED,
                title=f"Задача «{task.title}» обновлена",
                body=", ".join(changes.keys()),
                entity_type="task",
                entity_id=task.id,
                payload=changes,
            )
    # changed_keys, а не patch: assignee_ids/reg_no к этому месту уже извлечены pop()
    if changed_keys & {"incoming_number", "title", "assignee_ids", "reg_no"}:
        await incoming_sync(session, task, actor, reg_no=manual_reg_no)
    return task


async def change_status(session: AsyncSession, actor: User, task: Task, new_status: TaskStatus) -> Task:
    ensure_can_edit(actor, task)
    current = _status_value(task)
    if current == new_status.value:
        return task
    if new_status.value not in TASK_TRANSITIONS.get(current, set()):
        raise conflict(f"Недопустимый переход статуса: {current} → {new_status.value}")

    task.status = new_status
    task.completed_at = utcnow() if new_status == TaskStatus.DONE else None
    if new_status == TaskStatus.DONE:
        task.last_done_at = task.completed_at
        await record_occurrence_done(session, task, actor=actor)
    elif current == TaskStatus.DONE.value:
        # у повторяющейся задачи открылся обратно текущий период
        await retract_current_occurrence(session, task)

    session.add(
        TaskStatusHistory(
            task_id=task.id, from_status=current, to_status=new_status.value, actor_id=actor.id
        )
    )
    await session.flush()
    await notifications.notify_task_status(session, task, actor, current)
    await audit.write_audit(
        session, actor=actor, action="TASK_STATUS", entity_type="task", entity_id=task.id,
        changes={"from": current, "to": new_status.value},
    )
    await _sync_note(task, session)
    return task


# ------------------------------------------------- отметки «выполнено» (v1.2)
async def set_my_mark(session: AsyncSession, actor: User, task: Task) -> Task:
    """Исполнитель отмечает СВОЮ часть выполненной.

    Автоматика статусов:
      * первая отметка (задача NEW)            → IN_PROGRESS;
      * отметились ВСЕ исполнители             → DONE (completed_at = сейчас);
    """
    row = task.mark_of(actor.id)
    if row is None:
        raise forbidden("Отмечаться могут только исполнители задачи")
    if row.done_at is not None:
        raise conflict("Отметка «выполнено» уже поставлена")

    now = utcnow()
    row.done_at = now
    current = _status_value(task)

    all_done = bool(task.assignees) and all(a.done_at is not None for a in task.assignees)
    if all_done and current != TaskStatus.DONE.value:
        task.status = TaskStatus.DONE
        task.completed_at = now
        task.last_done_at = now   # для повторяющихся: когда выполняли в последний раз
        session.add(TaskStatusHistory(
            task_id=task.id, from_status=current, to_status=TaskStatus.DONE.value, actor_id=actor.id,
            note="Все исполнители отметили «выполнено»",
        ))
        await record_occurrence_done(session, task, actor=actor)
    elif current == TaskStatus.NEW.value:
        task.status = TaskStatus.IN_PROGRESS
        session.add(TaskStatusHistory(
            task_id=task.id, from_status=current, to_status=TaskStatus.IN_PROGRESS.value, actor_id=actor.id,
            note="Исполнитель отметил «выполнено»",
        ))
    await session.flush()

    done = sum(1 for a in task.assignees if a.done_at is not None)
    await notifications.notify_task_mark(session, task, actor, done=done, total=len(task.assignees), all_done=all_done)
    if all_done:
        await _sync_note(task, session)
    await audit.write_audit(
        session, actor=actor, action="TASK_MARK", entity_type="task", entity_id=task.id,
        changes={"user": actor.full_name, "mark": "done", "progress": f"{done}/{len(task.assignees)}"},
    )
    return task


async def clear_my_mark(session: AsyncSession, actor: User, task: Task) -> Task:
    """Снять свою отметку. Если задача была закрыта по отметкам — вернуть в IN_PROGRESS."""
    row = task.mark_of(actor.id)
    if row is None:
        raise forbidden("Отмечаться могут только исполнители задачи")
    if row.done_at is None:
        raise conflict("Отметки нет — снимать нечего")

    row.done_at = None
    current = _status_value(task)
    if current == TaskStatus.DONE.value:
        task.status = TaskStatus.IN_PROGRESS
        task.completed_at = None
        await retract_current_occurrence(session, task)
        session.add(TaskStatusHistory(
            task_id=task.id, from_status=current, to_status=TaskStatus.IN_PROGRESS.value, actor_id=actor.id,
            note="Снята отметка «выполнено»",
        ))
    await session.flush()

    done = sum(1 for a in task.assignees if a.done_at is not None)
    await notifications.notify_task_mark(session, task, actor, done=done, total=len(task.assignees), all_done=False)
    await audit.write_audit(
        session, actor=actor, action="TASK_MARK", entity_type="task", entity_id=task.id,
        changes={"user": actor.full_name, "mark": "cleared", "progress": f"{done}/{len(task.assignees)}"},
    )
    return task


# ---------------------------------------------------------------- комментарии
async def add_comment(session: AsyncSession, actor: User, task: Task, body: str) -> TaskComment:
    # событие истории добавляется после создания комментария ниже
    if not (can_see(actor, task)):
        raise forbidden("Нет доступа к этой задаче")
    comment = TaskComment(task_id=task.id, author_id=actor.id, body=body.strip())
    session.add(comment)
    await session.flush()
    await notifications.notify_task_comment(session, task, actor, body)
    log_event(session, task, actor, f"Добавлен комментарий: {body[:80]}")
    return comment


async def delete_task(session: AsyncSession, actor: User, task: Task) -> None:
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Удалять задачи могут Владелец и Администрация")
    title = task.title
    for f in list(task.files):
        _storage_mod.storage.delete(f.storage_path)
    await session.delete(task)
    await session.execute(
        delete(Notification).where(Notification.entity_type == "task", Notification.entity_id == task.id)
    )
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="TASK_DELETE", entity_type="task", entity_id=task.id, changes={"title": title}
    )


# ------------------------------------------------------------------ вложения
async def add_file(session: AsyncSession, actor: User, task: Task, file: UploadFile) -> TaskFile:
    """Прикрепить файл к задаче. Может любой участник (исполнитель/постановщик/админ)."""
    if not can_see(actor, task):
        raise forbidden("Нет доступа к этой задаче")
    if not (is_assignee(actor, task) or task.creator_id == actor.id or actor.role in (Role.OWNER, Role.ADMIN)):
        raise forbidden("Прикреплять файлы могут участники задачи")

    mime = (file.content_type or "").lower()
    if mime not in settings.allowed_task_file_mimes:
        raise bad_request("Недопустимый тип файла: " + (mime or "не определён"))

    stored = await _storage_mod.storage.save(file.file, file.filename or "file.bin", mime, allowed_mimes=settings.allowed_task_file_mimes)
    # v1.12: план автоматической PDF-копии определяется сразу, сама конвертация —
    # фоном после ответа (см. document_pdf.ensure_pdf_background в API)
    plan = document_pdf.plan_for(stored.mime_type)
    row = TaskFile(
        task_id=task.id,
        storage_path=stored.storage_path,
        original_name=stored.original_name,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        checksum=stored.checksum,
        pdf_status=plan.status,
        pdf_note=plan.note,
        uploaded_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(task, attribute_names=["files"])
    log_event(session, task, actor, f"Добавлен файл: {row.original_name}")
    await audit.write_audit(
        session, actor=actor, action="TASK_FILE", entity_type="task", entity_id=task.id,
        changes={"file": row.original_name, "size": row.size_bytes},
    )
    return row


async def delete_file(session: AsyncSession, actor: User, task: Task, file_id: uuid.UUID) -> None:
    row = next((f for f in task.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    is_manager = actor.role in (Role.OWNER, Role.ADMIN)
    if not (is_manager or row.uploaded_by_id == actor.id or task.creator_id == actor.id):
        raise forbidden("Удалить файл может загрузивший, постановщик или администрация")
    _storage_mod.storage.delete(row.storage_path)
    await session.delete(row)
    await session.flush()
    await session.refresh(task, attribute_names=["files"])
    log_event(session, task, actor, f"Удалён файл: {row.original_name}")
    await audit.write_audit(
        session, actor=actor, action="TASK_FILE_DELETE", entity_type="task", entity_id=task.id,
        changes={"file": row.original_name},
    )


# -------------------------------------------------------------------- отчёты
async def overdue_report(session: AsyncSession, actor: User) -> list[dict[str, Any]]:
    """Список просроченных задач в разрезе исполнителей (для планёрки/отчёта)."""
    now = utcnow()
    stmt = (
        _base_select()
        .where(and_(Task.deadline.is_not(None), Task.deadline < now, Task.status != TaskStatus.DONE))
        .order_by(Task.deadline.asc())
    )
    scoped = scope_filter(actor)
    if scoped is not None:
        stmt = stmt.where(scoped)
    tasks = (await session.execute(stmt)).scalars().all()
    result: list[dict[str, Any]] = []
    for t in tasks:
        deadline = _aware(t.deadline)
        result.append(
            {
                "id": str(t.id),
                "title": t.title,
                "assignees": [u.full_name for u in t.assignee_users] or ["Не назначен"],
                "assignee_ids": [str(x) for x in t.assignee_ids],
                "deadline": deadline.isoformat() if deadline else None,
                "overdue_by_days": int((now - deadline).total_seconds() // 86400) if deadline else None,
                "status": _status_value(t),
                "priority": t.priority,
            }
        )
    return result


EVENT_STATUS = "EVENT"   # запись истории без смены статуса: действие с задачей


def fmt_field(value) -> str:
    if value in (None, ""):
        return "—"
    return str(value)


def after_value(task: Task, field: str):
    return getattr(task, field, None)


def log_event(session: AsyncSession, task: Task, actor: User, note: str) -> None:
    """Строка истории «кто что сделал» (назначение, правки, файлы, комментарии)."""
    session.add(
        TaskStatusHistory(task_id=task.id, from_status=None, to_status=EVENT_STATUS, actor_id=actor.id, note=note[:500])
    )


# ------------------------------------------------------------------ журнал входящих
async def _reg_no_conflict(
    session: AsyncSession, reg_year: int, reg_no: int, exclude_id: uuid.UUID | None = None
) -> None:
    """409, если «номер регистрации» уже занят другим письмом этого года."""
    from app.models.incoming import IncomingLetter

    conds = [IncomingLetter.reg_year == reg_year, IncomingLetter.reg_no == reg_no]
    if exclude_id is not None:
        conds.append(IncomingLetter.id != exclude_id)
    dup = (await session.execute(select(IncomingLetter).where(and_(*conds)))).scalars().first()
    if dup is not None:
        raise conflict(
            f"Номер регистрации {reg_no}/{reg_year} уже занят письмом «{dup.subject[:80]}» "
            f"(вх. № {dup.incoming_number}). Укажите другой номер или оставьте поле пустым — "
            "тогда номер присвоится автоматически."
        )


async def incoming_sync(
    session: AsyncSession, task: Task, actor: User, reg_no: int | None = None
) -> None:
    """Задача с «входящим номером» ↔ запись журнала входящих писем.

    Номер указан  → создаём/обновляем запись (тема и ответственный берутся из задачи:
    ответственный — первый исполнитель, дата входящего — день создания записи).
    Номер убрали  → запись журнала удаляется (журнал — производное от поля задачи).

    reg_no — ручной «номер регистрации» из бумажного журнала: если указан, используется
    вместо автоматического (следующий по порядку); занятый номер → 409 с подсказкой.
    """
    from app.models.incoming import IncomingLetter

    number = (task.incoming_number or "").strip() or None
    row = (
        await session.execute(select(IncomingLetter).where(IncomingLetter.task_id == task.id))
    ).scalars().first()

    if not number:
        if row is not None:
            reg_label = f"{row.reg_no}-{row.reg_year}"
            await session.delete(row)
            await session.flush()
            await audit.write_audit(
                session, actor=actor, action="INCOMING_DELETE", entity_type="incoming",
                entity_id=row.id,
                changes={"reason": "task incoming_number removed", "reg": reg_label},
            )
        return

    today = date.today()
    first_assignee = task.assignees[0].user_id if task.assignees else None
    if row is None:
        reg_year = today.year
        if reg_no:
            await _reg_no_conflict(session, reg_year, reg_no)
            new_no = reg_no
        else:
            max_no = (
                await session.execute(
                    select(func.max(IncomingLetter.reg_no)).where(IncomingLetter.reg_year == reg_year)
                )
            ).scalar_one()
            new_no = (max_no or 0) + 1
        row = IncomingLetter(
            reg_year=reg_year,
            reg_no=new_no,
            incoming_date=today,
            incoming_number=number,
            subject=task.title[:500],
            responsible_id=first_assignee,
            task_id=task.id,
            created_by_id=actor.id,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            raise conflict("№ регистрации уже присвоен параллельно созданной записи — оставьте поле пустым и повторите")
        await audit.write_audit(
            session, actor=actor, action="INCOMING_CREATE", entity_type="incoming", entity_id=row.id,
            changes={
                "reg": f"{row.reg_no}-{reg_year}",
                "incoming_number": number,
                "source": "task",
                "task_id": str(task.id),
                "reg_manual": bool(reg_no),
            },
        )
    else:
        changed = False
        if row.incoming_number != number:
            row.incoming_number = number
            changed = True
        if row.subject != task.title[:500]:
            row.subject = task.title[:500]
            changed = True
        if first_assignee and row.responsible_id != first_assignee:
            row.responsible_id = first_assignee
            changed = True
        if reg_no and reg_no != row.reg_no:
            await _reg_no_conflict(session, row.reg_year, reg_no, exclude_id=row.id)
            row.reg_no = reg_no
            changed = True
        if changed:
            await session.flush()
            await audit.write_audit(
                session, actor=actor, action="INCOMING_UPDATE", entity_type="incoming", entity_id=row.id,
                changes={
                    "source": "task",
                    "incoming_number": number,
                    "reg": f"{row.reg_no}/{row.reg_year}",
                },
            )


async def _sync_note(task: Task, session: AsyncSession) -> None:
    """Связь с журналом служебных записок: закрытая задача завершает записку.

    Импорт локальный — service_notes импортирует tasks (циклическая зависимость).
    """
    from app.services import service_notes

    await service_notes.sync_completed_from_task(session, task)
