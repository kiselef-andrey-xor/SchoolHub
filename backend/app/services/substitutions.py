"""Сервис замещений (модуль 3 ТЗ).

Администрация создаёт запись: отсутствующий → замещающий, причина, период.
Флаги «передать задачи / планы / ВШК» описывают, ЧТО будет передано замещающему.

Порядок передачи (v1.11 — исправлен по замечанию Владельца):
  * при создании объекты НЕ трогаются: замещающий ещё не согласился, а вернуть
    задачи отсутствующему при отмене система раньше не умела;
  * передача выполняется в момент ПОДТВЕРЖДЕНИЯ замещающим (`transferred_at`);
  * при отмене, завершении и удалении строки переданное ВОЗВРАЩАется
    отсутствующему (`transferred_at` снова NULL) — открытые задачи, ссылки
    ответственных в планах и мероприятиях ВШК;
  * период можно ПРОДЛИТЬ (больничный продлили) — `extend_sub`.

Переназначение:
  * tasks   — исполнитель from → to (TaskAssignee; история + уведомления);
  * plans   — ссылки ответственных "<from>" в JSON-документе недельных планов → "<to>";
  * vshk    — то же в resp мероприятий годовых планов ВШК.
Каждое перенаписывание фиксируется в аудите понятной формулировкой и в sub.note (сводка).
Замещающий подтверждает принятие ПРОСТЫМ КЛИКОМ; журнал замещений + CSV-экспорт.

Права (v1.11): журнал целиком — Владелец, администрация и «кадровик» (персональный
грант Владельца `users.substitutions_access`); остальные сотрудники — только свои
замещения (как отсутствующий или замещающий), иначе они не смогли бы подтвердить
принятие. Удаляют строки Владелец и кадровик.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import Role, SubstitutionStatus, TaskStatus
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.plan import Plan
from app.models.substitution import Substitution
from app.models.task import Task, TaskAssignee, TaskStatusHistory
from app.models.user import User
from app.models.vshk import VshkItem
from app.services import audit, notifications

OPEN_TASK_STATUSES = [TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]
LIVE_STATUSES = [SubstitutionStatus.ACTIVE, SubstitutionStatus.CONFIRMED]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------- права
def is_manager(actor: User) -> bool:
    """Оформляет/продлевает/завершает/отменяет замещение: Владелец и администрация."""
    return actor.role in (Role.OWNER, Role.ADMIN)


def is_hr(actor: User) -> bool:
    """«Кадровик» — персональный грант Владельца (должность значения не имеет)."""
    return bool(getattr(actor, "substitutions_access", False))


def sees_full_journal(actor: User) -> bool:
    return is_manager(actor) or is_hr(actor)


def can_delete(actor: User) -> bool:
    """Строку замещения удаляет Владелец или назначенный им кадровик."""
    return actor.role == Role.OWNER or is_hr(actor)


async def get_sub(session: AsyncSession, sub_id: uuid.UUID, actor: User) -> Substitution:
    sub = await session.get(Substitution, sub_id)
    if sub is None:
        raise not_found("Замещение")
    if not sees_full_journal(actor) and \
            sub.absent_user_id != actor.id and sub.substitute_user_id != actor.id:
        raise forbidden("Нет доступа к этому замещению")
    return sub


async def has_live_participation(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Есть ли у сотрудника активное/подтверждённое замещение (он отсутствует или замещает).

    Нужна для `/auth/me`: участнику выдаётся capability `substitutions:read`, иначе
    он не сможет открыть раздел и подтвердить принятие (а передача объектов с v1.11
    выполняется только после подтверждения).
    """
    row = (await session.execute(
        select(Substitution.id).where(
            Substitution.status.in_(LIVE_STATUSES),
            or_(Substitution.absent_user_id == user_id, Substitution.substitute_user_id == user_id),
        ).limit(1)
    )).first()
    return row is not None


# ------------------------------------------------------------------ создание
async def create_sub(session: AsyncSession, actor: User, *, absent_id: uuid.UUID,
                     substitute_id: uuid.UUID, reason: str, start: datetime, end: datetime | None,
                     transfer_tasks: bool, transfer_plans: bool, transfer_vshk: bool) -> Substitution:
    if not is_manager(actor):
        raise forbidden("Оформлять замещение может администрация")
    if absent_id == substitute_id:
        raise bad_request("Замещающий не может совпадать с отсутствующим")
    absent = await session.get(User, absent_id)
    subst = await session.get(User, substitute_id)
    for u, label in ((absent, "Отсутствующий"), (subst, "Замещающий")):
        if u is None or u.is_deleted or not u.is_active:
            raise bad_request(f"{label}: сотрудник не найден или недоступен")

    start_a, end_a = _aware(start), _aware(end)
    if end_a and end_a <= start_a:
        raise bad_request("Конец периода раньше начала")

    # пересечение активных замещений одного и того же человека запрещено
    overlapping = (await session.execute(
        select(Substitution).where(
            Substitution.absent_user_id == absent_id,
            Substitution.status.in_(LIVE_STATUSES),
            Substitution.start_date <= (end_a or _now()),
            or_(Substitution.end_date.is_(None), Substitution.end_date >= start_a),
        )
    )).scalars().first()
    if overlapping:
        raise conflict("На этот период у сотрудника уже есть активное замещение")

    sub = Substitution(
        absent_user_id=absent_id, substitute_user_id=substitute_id, reason=reason.strip(),
        start_date=start_a, end_date=end_a,
        transfer_tasks=transfer_tasks, transfer_plans=transfer_plans, transfer_vshk=transfer_vshk,
        status=SubstitutionStatus.ACTIVE, initiator_id=actor.id,
        note=_pending_note(transfer_tasks, transfer_plans, transfer_vshk),
    )
    session.add(sub)
    await session.flush()

    await notifications.push(
        session, user_id=substitute_id, type_="SUBSTITUTION_ASSIGNED",
        title=f"Вам назначено замещение: {absent.full_name}",
        body=f"{reason.strip()} · период с {start_a.strftime('%d.%m.%Y')}"
             f"{' по ' + end_a.strftime('%d.%m.%Y') if end_a else ''}. Подтвердите принятие одним кликом — "
             f"тогда вам будут переданы: {_objects_ru(transfer_tasks, transfer_plans, transfer_vshk)}.",
        entity_type="substitution", entity_id=sub.id,
    )
    await notifications.push(
        session, user_id=absent_id, type_="SUBSTITUTION_INFO",
        title=f"Оформлено замещение на вас: {subst.full_name}",
        body=sub.note, entity_type="substitution", entity_id=sub.id,
    )
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_CREATE", entity_type="substitution", entity_id=sub.id,
        changes={"absent": absent.full_name, "substitute": subst.full_name,
                 "period": (f"{start_a:%d.%m.%Y}–{end_a:%d.%m.%Y}" if end_a else f"{start_a:%d.%m.%Y}–бессрочно"),
                 "flags": {"tasks": transfer_tasks, "plans": transfer_plans, "vshk": transfer_vshk},
                 "result": sub.note},
    )
    return sub


def _objects_ru(tasks: bool, plans: bool, vshk: bool) -> str:
    parts = []
    if tasks:
        parts.append("задачи")
    if plans:
        parts.append("планы")
    if vshk:
        parts.append("мероприятия ВШК")
    return ", ".join(parts) if parts else "ничего (замещение без передачи объектов)"


def _pending_note(tasks: bool, plans: bool, vshk: bool) -> str:
    return (f"ожидает подтверждения замещающим; к передаче: {_objects_ru(tasks, plans, vshk)}"
            if (tasks or plans or vshk) else "без передачи объектов")


# ------------------------------------------------- переназначение объектов
def _swap_refs(refs: list, a: str, b: str) -> list | None:
    """Список ответственных: заменить `a` на `b` без дублей. None — изменений нет."""
    out: list = []
    changed = False
    has_b = any(str(x) == b for x in refs)
    for x in refs:
        if str(x) == a:
            changed = True
            if has_b:
                continue  # b уже среди ответственных — просто убираем a
            out.append(b)
            has_b = True
        else:
            out.append(x)
    return out if changed else None


async def _reassign_tasks(session: AsyncSession, sub: Substitution, *, from_id: uuid.UUID,
                          to_id: uuid.UUID, actor_id: uuid.UUID | None, note: str) -> tuple[int, list[str]]:
    """Исполнитель from → to по всем незакрытым задачам (в обе стороны: передача и возврат)."""
    tasks = (await session.execute(
        select(Task).join(TaskAssignee, TaskAssignee.task_id == Task.id)
        .where(TaskAssignee.user_id == from_id, Task.status.in_(OPEN_TASK_STATUSES))
        .options(selectinload(Task.assignees), selectinload(Task.history))
        .distinct()
    )).scalars().all()

    count = 0
    titles: list[str] = []
    for task in tasks:
        # присваиваем новую коллекцию целиком: append/remove к ленивой коллекции внутри
        # async-сессии вызывал lazy-загрузку вне greenlet (500 MissingGreenlet)
        new_assignees: list[TaskAssignee] = []
        for a in list(task.assignees):
            if a.user_id == from_id:
                if any(x.user_id == to_id for x in task.assignees):
                    continue  # получатель уже исполнитель — просто убираем строку отправителя
                a.user_id = to_id
            new_assignees.append(a)
        task.assignees = new_assignees
        task.history.append(TaskStatusHistory(
            task_id=task.id, from_status=str(task.status.value), to_status=str(task.status.value),
            actor_id=actor_id, note=note,
        ))
        count += 1
        titles.append(task.title)
    if count:
        await session.flush()
    return count, titles


async def _reassign_plans(session: AsyncSession, *, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
    """Ссылки ответственного from → to в content планов недель (duty + resp пунктов)."""
    plans = (await session.execute(select(Plan))).scalars().all()
    a, b = str(from_id), str(to_id)
    touched = 0
    for plan in plans:
        content = plan.content or {}
        changed = False
        duty = content.get("duty") or {}
        if str(duty.get("teacher_id") or "") == a:
            duty["teacher_id"] = b
            content["duty"] = duty
            changed = True
        for _, item in list(plan.iter_items()):
            refs = item.get("resp") or []
            swapped = _swap_refs(refs, a, b)
            if swapped is not None:
                item["resp"] = swapped
                changed = True
        if changed:
            plan.content = content  # JSONB mutation → явное присваивание для dirty-tracking
            touched += 1
    if touched:
        await session.flush()
    return touched


async def _reassign_vshk(session: AsyncSession, *, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
    a, b = str(from_id), str(to_id)
    all_items = (await session.execute(select(VshkItem))).scalars().all()
    touched = 0
    for it in all_items:
        swapped = _swap_refs([str(x) for x in (it.resp or [])], a, b)
        if swapped is not None:
            it.resp = swapped
            touched += 1
    if touched:
        await session.flush()
    return touched


async def _apply_transfer(session: AsyncSession, sub: Substitution, *, actor_id: uuid.UUID | None) -> str:
    """Передача объектов замещающему (только после подтверждения). Возвращает сводку."""
    summary: list[str] = []
    if sub.transfer_tasks:
        count, titles = await _reassign_tasks(
            session, sub, from_id=sub.absent_user_id, to_id=sub.substitute_user_id, actor_id=actor_id,
            note=f"Замещение {sub.id}: исполнитель изменён с «{sub.absent_user.full_name}» "
                 f"на «{sub.substitute_user.full_name}»",
        )
        summary.append(f"передано задач: {count}")
        if count:
            await notifications.push(
                session, user_id=sub.substitute_user_id, type_="SUBSTITUTION_TASKS",
                title=f"По замещению передано задач: {count}",
                body="Задачи, где вы теперь исполнитель, видны в вашем списке: "
                     + "; ".join(titles[:5]) + ("…" if len(titles) > 5 else ""),
                entity_type="substitution", entity_id=sub.id,
            )
    if sub.transfer_plans:
        n = await _reassign_plans(session, from_id=sub.absent_user_id, to_id=sub.substitute_user_id)
        summary.append(f"планов переписано: {n}")
    if sub.transfer_vshk:
        n = await _reassign_vshk(session, from_id=sub.absent_user_id, to_id=sub.substitute_user_id)
        summary.append(f"мероприятий ВШК переписано: {n}")

    if summary:
        sub.transferred_at = _now()
        return "; ".join(summary)
    sub.transferred_at = None
    return "без передачи объектов"


async def _revert_transfer(session: AsyncSession, sub: Substitution, *, actor_id: uuid.UUID | None,
                           reason: str) -> str | None:
    """Возврат переданных объектов отсутствующему. None — возвращать нечего.

    Возвращаются только НЕзакрытые задачи и те ссылки ответственных, где сейчас
    указан замещающий: выполненное/отменённое задним числом не переписывается.
    """
    if sub.transferred_at is None:
        return None
    summary: list[str] = []
    if sub.transfer_tasks:
        count, titles = await _reassign_tasks(
            session, sub, from_id=sub.substitute_user_id, to_id=sub.absent_user_id, actor_id=actor_id,
            note=f"Замещение {sub.id} ({reason}): исполнитель возвращён с «{sub.substitute_user.full_name}» "
                 f"на «{sub.absent_user.full_name}»",
        )
        summary.append(f"возвращено задач: {count}")
        if count:
            listed = "; ".join(titles[:5]) + ("…" if len(titles) > 5 else "")
            await notifications.push(
                session, user_id=sub.absent_user_id, type_="SUBSTITUTION_INFO",
                title=f"Задачи возвращены вам ({reason}): {count}",
                body=listed, entity_type="substitution", entity_id=sub.id,
            )
            await notifications.push(
                session, user_id=sub.substitute_user_id, type_="SUBSTITUTION_INFO",
                title=f"Сняты задачи по замещению ({reason}): {count}",
                body=listed, entity_type="substitution", entity_id=sub.id,
            )
    if sub.transfer_plans:
        n = await _reassign_plans(session, from_id=sub.substitute_user_id, to_id=sub.absent_user_id)
        summary.append(f"планов возвращено: {n}")
    if sub.transfer_vshk:
        n = await _reassign_vshk(session, from_id=sub.substitute_user_id, to_id=sub.absent_user_id)
        summary.append(f"мероприятий ВШК возвращено: {n}")

    sub.transferred_at = None
    return "; ".join(summary) if summary else None


def _append_note(sub: Substitution, text: str) -> None:
    sub.note = ((sub.note + "; " + text) if sub.note else text)[:2000]


# --------------------------------------------------------------------- lifecycle
async def confirm_sub(session: AsyncSession, actor: User, sub: Substitution) -> Substitution:
    """Замещающий подтверждает простым кликом — и ТОЛЬКО тогда объекты передаются."""
    if sub.substitute_user_id != actor.id:
        raise forbidden("Подтверждать принятие может только замещающий")
    if sub.status != SubstitutionStatus.ACTIVE:
        raise conflict(f"Замещение уже {sub.status.value.lower()} — подтверждение не требуется")
    sub.status = SubstitutionStatus.CONFIRMED
    sub.confirmed_at = _now()

    summary = await _apply_transfer(session, sub, actor_id=actor.id)
    sub.note = summary
    await session.flush()

    await notifications.push(
        session, user_id=sub.initiator_id, type_="SUBSTITUTION_CONFIRMED",
        title=f"Замещение подтверждено: {sub.substitute_user.full_name}",
        body=f"Вместо {sub.absent_user.full_name} с {sub.start_date:%d.%m.%Y}. {summary}",
        entity_type="substitution", entity_id=sub.id,
    )
    await notifications.push(
        session, user_id=sub.absent_user_id, type_="SUBSTITUTION_INFO",
        title=f"Замещение подтверждено: {sub.substitute_user.full_name}",
        body=summary, entity_type="substitution", entity_id=sub.id,
    )
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_CONFIRM", entity_type="substitution", entity_id=sub.id,
        changes={"substitute": sub.substitute_user.full_name, "result": summary},
    )
    return sub


async def end_sub(session: AsyncSession, actor: User, sub: Substitution) -> Substitution:
    """Завершение (вручную администрацией). Переданные объекты ВОЗВРАЩАЮТСЯ отсутствующему."""
    if not is_manager(actor):
        raise forbidden("Завершать замещение может администрация")
    if sub.status in (SubstitutionStatus.ENDED, SubstitutionStatus.CANCELLED):
        raise conflict("Замещение уже закрыто")
    sub.status = SubstitutionStatus.ENDED
    sub.ended_at = _now()
    returned = await _revert_transfer(session, sub, actor_id=actor.id, reason="замещение завершено")
    if returned:
        _append_note(sub, f"завершено: {returned}")
    await session.flush()
    await _notify_closed(session, sub, "Замещение завершено", returned)
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_END", entity_type="substitution", entity_id=sub.id,
        changes={"absent": sub.absent_user.full_name, "substitute": sub.substitute_user.full_name,
                 "returned": returned or "объекты не передавались"},
    )
    return sub


async def cancel_sub(session: AsyncSession, actor: User, sub: Substitution, comment: str | None) -> Substitution:
    """Отмена: переданные объекты ВОЗВРАЩАЮТСЯ отсутствующему."""
    if not is_manager(actor):
        raise forbidden("Отменять замещение может администрация")
    if sub.status in (SubstitutionStatus.ENDED, SubstitutionStatus.CANCELLED):
        raise conflict("Замещение уже закрыто")
    sub.status = SubstitutionStatus.CANCELLED
    sub.ended_at = _now()
    returned = await _revert_transfer(session, sub, actor_id=actor.id, reason="замещение отменено")
    if comment:
        _append_note(sub, "отменено: " + comment)
    if returned:
        _append_note(sub, f"возвращено: {returned}")
    await session.flush()
    await _notify_closed(session, sub, "Замещение отменено", returned, comment)
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_CANCEL", entity_type="substitution", entity_id=sub.id,
        changes={"comment": comment, "returned": returned or "объекты не передавались"},
    )
    return sub


async def _notify_closed(session: AsyncSession, sub: Substitution, title: str,
                         returned: str | None, comment: str | None = None) -> None:
    body = comment or ""
    tail = (f" Возвращено отсутствующему: {returned}." if returned
            else " Объекты не передавались — возвращать нечего.")
    for uid in (sub.absent_user_id, sub.substitute_user_id):
        await notifications.push(
            session, user_id=uid, type_="SUBSTITUTION_INFO", title=title,
            body=(body + tail).strip() or title,
            entity_type="substitution", entity_id=sub.id,
        )


async def extend_sub(session: AsyncSession, actor: User, sub: Substitution, *,
                     new_end: datetime, comment: str | None = None) -> Substitution:
    """Продление периода (больничный продлили): новый end_date, объекты не трогаем."""
    if not is_manager(actor):
        raise forbidden("Продлевать замещение может администрация")
    if sub.status not in LIVE_STATUSES:
        raise conflict("Продлить можно только активное замещение")
    end_a = _aware(new_end)
    if end_a is None:
        raise bad_request("Укажите новую дату окончания")
    start_a = _aware(sub.start_date) or _now()
    if end_a <= start_a:
        raise bad_request("Конец периода раньше начала")
    if sub.end_date and end_a <= _aware(sub.end_date):
        raise bad_request("Новая дата должна быть позже текущего окончания периода")
    if end_a < _now():
        raise bad_request("Новая дата окончания уже прошла")

    # продление не должно пересекаться с другим активным замещением того же человека
    overlapping = (await session.execute(
        select(Substitution).where(
            Substitution.id != sub.id,
            Substitution.absent_user_id == sub.absent_user_id,
            Substitution.status.in_(LIVE_STATUSES),
            Substitution.start_date <= end_a,
            or_(Substitution.end_date.is_(None), Substitution.end_date >= start_a),
        )
    )).scalars().first()
    if overlapping:
        raise conflict("На продлённый период у сотрудника уже есть другое активное замещение")

    old_end = _aware(sub.end_date)
    sub.end_date = end_a
    _append_note(sub, f"продлено до {end_a:%d.%m.%Y}" + (f" ({comment.strip()})" if comment else ""))
    await session.flush()

    period = f"{sub.start_date:%d.%m.%Y}–{end_a:%d.%m.%Y}"
    for uid in (sub.absent_user_id, sub.substitute_user_id):
        await notifications.push(
            session, user_id=uid, type_="SUBSTITUTION_INFO", title="Замещение продлено",
            body=(f"Период: {period} (было: {old_end:%d.%m.%Y}). " if old_end else f"Период: {period}. ")
                 + (comment or ""),
            entity_type="substitution", entity_id=sub.id,
        )
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_EXTEND", entity_type="substitution", entity_id=sub.id,
        changes={"absent": sub.absent_user.full_name, "substitute": sub.substitute_user.full_name,
                 "old_end": f"{old_end:%d.%m.%Y}" if old_end else "бессрочно",
                 "new_end": f"{end_a:%d.%m.%Y}", "comment": comment},
    )
    return sub


async def delete_sub(session: AsyncSession, actor: User, sub: Substitution) -> None:
    """Удаление строки замещения (Владелец или кадровик).

    Если объекты были переданы и замещение ещё живо — сначала возвращаем их
    отсутствующему, чтобы задачи не «зависли» на замещающем без основания.
    """
    if not can_delete(actor):
        raise forbidden("Удалять строки замещений может Владелец или назначенный им кадровик")
    returned = None
    if sub.status in LIVE_STATUSES:
        returned = await _revert_transfer(session, sub, actor_id=actor.id, reason="строка замещения удалена")
    label = (f"{sub.absent_user.full_name} → {sub.substitute_user.full_name} "
             f"с {sub.start_date:%d.%m.%Y}" + (f" по {sub.end_date:%d.%m.%Y}" if sub.end_date else ""))
    status = str(sub.status.value)
    await session.delete(sub)
    await session.flush()
    await notifications.push(
        session, user_id=sub.substitute_user_id, type_="SUBSTITUTION_INFO",
        title="Запись о замещении удалена",
        body=label + (f". Возвращено отсутствующему: {returned}." if returned else ""),
        entity_type="substitution", entity_id=None,
    )
    await audit.write_audit(
        session, actor=actor, action="SUBSTITUTION_DELETE", entity_type="substitution", entity_id=sub.id,
        changes={"record": label, "status": status, "returned": returned or "объекты не передавались"},
    )


async def auto_expire(session: AsyncSession) -> int:
    """Домашняя работа housekeeping: закрыть замещения, у которых прошёл end_date.

    Закрытие = то же, что ручное «Завершить»: переданные объекты возвращаются
    отсутствующему (он вышел на работу).
    """
    now = _now()
    rows = (await session.execute(
        select(Substitution).where(
            Substitution.status.in_(LIVE_STATUSES),
            Substitution.end_date.is_not(None),
            Substitution.end_date < now,
        )
    )).scalars().all()
    for sub in rows:
        sub.status = SubstitutionStatus.ENDED
        sub.ended_at = now
        returned = await _revert_transfer(session, sub, actor_id=sub.initiator_id,
                                          reason="период замещения истёк")
        if returned:
            _append_note(sub, f"завершено автоматически: {returned}")
    if rows:
        await session.flush()
    return len(rows)


async def list_subs(session: AsyncSession, actor: User, *, status: str | None = None,
                    user_id: uuid.UUID | None = None, ongoing: bool | None = None) -> list[Substitution]:
    stmt = select(Substitution)
    if not sees_full_journal(actor):
        # сотрудник видит только свои замещения (как отсутствующий или замещающий)
        stmt = stmt.where(or_(Substitution.absent_user_id == actor.id,
                              Substitution.substitute_user_id == actor.id))
    if status:
        stmt = stmt.where(Substitution.status == status)
    if user_id:
        stmt = stmt.where(or_(Substitution.absent_user_id == user_id,
                              Substitution.substitute_user_id == user_id))
    if ongoing:
        stmt = stmt.where(Substitution.status.in_(LIVE_STATUSES))
    stmt = stmt.order_by(Substitution.start_date.desc())
    return list((await session.execute(stmt)).scalars().all())
