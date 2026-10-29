"""Сервис Helpdesk: заявки сотрудников в техническую поддержку школы (v1.12).

Логика модуля спроектирована с нуля под школьную реальность: «зажевало принтер»,
«не включается компьютер», «нет интернета на интерактивной доске», «не работает
документ-камера». Отсюда три принципиальных решения.

1. ОЧЕРЕДЬ, А НЕ НАЗНАЧЕНИЕ
   Заявка не назначается на конкретного человека сверху: её видят ВСЕ системные
   администраторы (грант Владельца `users.helpdesk_access`) и сами берут в работу.
   В школе сисадмин один-два, и «назначенная, но не взятая» заявка просто
   потерялась бы. Кто первый нажал «Взять в работу» — тот и исполнитель;
   переназначить можно явно (уехал, заболел).

2. ПОДТВЕРЖДЕНИЕ АВТОРА
   Выполнение — не конец: заявка переходит в RESOLVED, а закрывает её автор.
   Так «починили» означает «у учителя действительно работает». Если автор молчит
   TICKET_AUTOCLOSE_DAYS дней, заявку закрывает housekeeping (auto_closed=True) —
   иначе очередь закрытых заявок никогда не сходилась бы.

3. НОРМАТИВ РЕАКЦИИ, А НЕ СРОК ПОЧИНКИ
   SLA (`app.enums.TICKET_SLA_HOURS`) меряет время от создания до ВЗЯТИЯ в работу:
   срок ремонта зависит от запчастей и договора с подрядчиком, а вот отреагировать
   администратор обязан. Норматив копируется в заявку при создании (`sla_hours`),
   поэтому правка значений в коде не переписывает историю старых заявок.

Права
-----
  * создать заявку и видеть СВОИ — любой сотрудник;
  * видеть всю очередь, брать в работу, выполнять, отклонять, удалять —
    системные администраторы (грант) и Владелец;
  * администрация (ADMIN) видит очередь ЧТЕНИЕМ: завучу важно понимать, почему
    срывается урок, но чинить принтер он не должен.

Каждое изменение статуса пишет служебную отметку в ленту (`kind=SYSTEM`) и
событие в журнал — по заявке всегда видно, кто и когда её трогал.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    TICKET_SLA_HOURS,
    TICKET_TRANSITIONS,
    Role,
    TicketCategory,
    TicketFileKind,
    TicketMessageKind,
    TicketPriority,
    TicketStatus,
)
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.helpdesk import HelpdeskFile, HelpdeskMessage, HelpdeskTicket
from app.models.user import User
from app.services import audit, notifications
from app.types import enum_value

#: Тип сущности для уведомлений (NavigationPage/AppLayout ходят по нему в раздел)
ENTITY_TYPE = "helpdesk_ticket"

#: Типы уведомлений Helpdesk
NOTIFY_NEW = "TICKET_NEW"
NOTIFY_TAKEN = "TICKET_TAKEN"
NOTIFY_RESOLVED = "TICKET_RESOLVED"
NOTIFY_CLOSED = "TICKET_CLOSED"
NOTIFY_REOPENED = "TICKET_REOPENED"
NOTIFY_REJECTED = "TICKET_REJECTED"
NOTIFY_CANCELLED = "TICKET_CANCELLED"
NOTIFY_MESSAGE = "TICKET_MESSAGE"
NOTIFY_SLA = "TICKET_SLA_BREACH"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Приводит naive-значение из БД к UTC (SQLite не хранит часовой пояс)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean(value: str | None, limit: int) -> str | None:
    """Текст поля: обрезка пробелов и длины; пустая строка → None (в БД не храним «»)."""
    text = (value or "").strip()[:limit]
    return text or None


def _minutes(a: datetime | None, b: datetime | None) -> int | None:
    start, end = _aware(a), _aware(b)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() // 60))


# ------------------------------------------------------------------- доступы
def is_staff(actor: User) -> bool:
    """Системный администратор: Владелец или сотрудник с грантом Владельца.

    Роль значения не имеет — сисадмином может быть учитель информатики (TEACHER).
    """
    return actor.role == Role.OWNER or bool(getattr(actor, "helpdesk_access", False))


def can_view_all(actor: User) -> bool:
    """Кто видит всю очередь: сисадмины (работа) и администрация (контроль)."""
    return is_staff(actor) or actor.role == Role.ADMIN


def can_operate(actor: User) -> bool:
    """Кто может брать в работу, выполнять и отклонять.

    Администрация НАМЕРЕННО не входит: право видеть очередь не означает права
    её закрывать. Иначе завуч «выполнил» бы заявку, которую никто не чинил.
    """
    return is_staff(actor)


def apply_visibility(stmt, actor: User):
    """Ограничивает запрос заявок кругом видимости актора."""
    if can_view_all(actor):
        return stmt
    return stmt.where(HelpdeskTicket.author_id == actor.id)


async def staff_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Кому адресовать уведомление о новой заявке: Владелец + все сисадмины."""
    rows = (await session.execute(
        select(User.id).where(
            User.is_active.is_(True),
            User.is_deleted.is_(False),
            or_(User.role == Role.OWNER.value, User.helpdesk_access.is_(True)),
        )
    )).scalars().all()
    return list(rows)


# ----------------------------------------------------------------- SLA и сроки
def sla_deadline(ticket: HelpdeskTicket) -> datetime | None:
    """Крайний срок РЕАКЦИИ (взятия в работу). None — норматив не задан."""
    hours = ticket.sla_hours
    if not hours:
        return None
    return _aware(ticket.created_at) + timedelta(hours=hours)


def is_sla_breached(ticket: HelpdeskTicket, *, at: datetime | None = None) -> bool:
    """Просрочена ли реакция: заявка открыта, а норматив уже истёк.

    Считается при чтении и в БД не хранится: флаг устаревал бы сам по себе
    и требовал cron'а (тот же подход, что у OVERDUE в задачах).
    """
    if ticket.status.value not in (TicketStatus.NEW.value,):
        # заявку взяли в работу — реакция состоялась, норматив закрыт
        return False
    deadline = sla_deadline(ticket)
    if deadline is None:
        return False
    return (at or _now()) > deadline


def metrics(ticket: HelpdeskTicket) -> dict:
    """Вычисляемые показатели заявки для Out-схемы."""
    return {
        "sla_deadline": sla_deadline(ticket),
        "sla_breached": is_sla_breached(ticket),
        "reaction_minutes": _minutes(ticket.created_at, ticket.taken_at),
        "work_minutes": _minutes(ticket.taken_at, ticket.resolved_at or ticket.closed_at),
    }


# ------------------------------------------------------------ state machine
def _ensure_transition(ticket: HelpdeskTicket, target: TicketStatus) -> None:
    current = enum_value(ticket.status)
    allowed = TICKET_TRANSITIONS.get(current, set())
    if target.value not in allowed:
        raise conflict(
            f"Заявку нельзя перевести из состояния «{STATUS_RU[current]}» "
            f"в «{STATUS_RU[target.value]}». Доступные действия: "
            f"{', '.join(STATUS_RU[a] for a in sorted(allowed)) or 'нет'}"
        )


STATUS_RU = {
    TicketStatus.NEW.value: "новая",
    TicketStatus.IN_PROGRESS.value: "в работе",
    TicketStatus.RESOLVED.value: "выполнена",
    TicketStatus.CLOSED.value: "закрыта",
    TicketStatus.REJECTED.value: "отклонена",
    TicketStatus.CANCELLED.value: "отозвана",
}

CATEGORY_RU = {
    TicketCategory.PRINTER.value: "принтер / МФУ / сканер",
    TicketCategory.COMPUTER.value: "компьютер / ноутбук",
    TicketCategory.NETWORK.value: "интернет / сеть",
    TicketCategory.BOARD.value: "интерактивная доска",
    TicketCategory.PROJECTOR.value: "проектор / экран",
    TicketCategory.DOC_CAMERA.value: "документ-камера",
    TicketCategory.AUDIO.value: "звук / микрофон",
    TicketCategory.SOFTWARE.value: "программы",
    TicketCategory.ACCOUNT.value: "доступы и пароли",
    TicketCategory.PHONE.value: "телефон",
    TicketCategory.OTHER.value: "прочее",
}

PRIORITY_RU = {
    TicketPriority.LOW.value: "низкий",
    TicketPriority.NORMAL.value: "обычный",
    TicketPriority.HIGH.value: "высокий",
    TicketPriority.URGENT.value: "срочный",
}


def status_label(status) -> str:
    """Русская подпись статуса заявки — единственная точка перевода для API и выгрузок."""
    return STATUS_RU.get(enum_value(status), enum_value(status) or "—")


def category_label(category) -> str:
    return CATEGORY_RU.get(enum_value(category), enum_value(category) or "—")


def priority_label(priority) -> str:
    return PRIORITY_RU.get(enum_value(priority), enum_value(priority) or "—")


def directory() -> dict:
    """Справочники для формы заявки — один источник подписей и для API, и для UI."""
    return {
        "categories": [{"value": v, "label": CATEGORY_RU[v]} for v in TicketCategory],
        "priorities": [{"value": v, "label": PRIORITY_RU[v]} for v in TicketPriority],
        "statuses": [{"value": v, "label": STATUS_RU[v]} for v in TicketStatus],
        "sla_hours": dict(TICKET_SLA_HOURS),
    }


# ------------------------------------------------------------------ получение
async def get_ticket(session: AsyncSession, ticket_id: uuid.UUID, actor: User) -> HelpdeskTicket:
    ticket = await session.get(HelpdeskTicket, ticket_id)
    if ticket is None:
        raise not_found("Заявка")
    if not can_see(ticket, actor):
        raise forbidden("Нет доступа к этой заявке")
    return ticket


def can_see(ticket: HelpdeskTicket, actor: User) -> bool:
    if can_view_all(actor):
        return True
    return ticket.author_id == actor.id or ticket.assignee_id == actor.id


async def _next_number(session: AsyncSession, year: int) -> int:
    current = await session.scalar(
        select(func.max(HelpdeskTicket.number)).where(HelpdeskTicket.year == year)
    )
    return (current or 0) + 1


async def _log_event(session: AsyncSession, ticket: HelpdeskTicket, actor: User | None, text: str) -> None:
    """Служебная отметка в ленте заявки (kind=SYSTEM) — не редактируется и не удаляется."""
    session.add(HelpdeskMessage(
        ticket_id=ticket.id, author_id=actor.id if actor else None,
        kind=TicketMessageKind.SYSTEM.value, body=text[:4000],
    ))


# -------------------------------------------------------------------- создание
async def create_ticket(session: AsyncSession, actor: User, *, title: str, description: str,
                        category: TicketCategory, priority: TicketPriority,
                        location: str | None, contact_phone: str | None,
                        equipment_id: uuid.UUID | None) -> HelpdeskTicket:
    """Заявку подаёт любой сотрудник. Назначения нет — она попадает в общую очередь."""
    title = (title or "").strip()
    description = (description or "").strip()
    if len(title) < 3:
        raise bad_request("Слишком короткая тема заявки (минимум 3 символа)")
    if len(description) < 5:
        raise bad_request("Опишите проблему подробнее (минимум 5 символов)")

    equipment = None
    if equipment_id is not None:
        from app.models.equipment import EquipmentItem

        equipment = await session.get(EquipmentItem, equipment_id)
        if equipment is None:
            raise bad_request("Единица оборудования не найдена")

    year = _now().year
    ticket = HelpdeskTicket(
        number=await _next_number(session, year),
        year=year,
        author_id=actor.id,
        title=title[:300],
        description=description,
        category=category,
        priority=priority,
        status=TicketStatus.NEW,
        location=_clean(location, 200),
        contact_phone=_clean(contact_phone, 40),
        equipment_id=equipment.id if equipment else None,
        # норматив фиксируется на момент создания — см. sla_hours в модели
        sla_hours=TICKET_SLA_HOURS.get(enum_value(priority)),
    )
    session.add(ticket)
    await session.flush()
    await _log_event(
        session, ticket, actor,
        f"Заявка создана. Категория: {CATEGORY_RU[enum_value(category)]}, "
        f"приоритет: {PRIORITY_RU[enum_value(priority)]}. "
        f"Норматив реакции: {ticket.sla_hours or '—'} ч.",
    )
    await session.flush()

    staff = await staff_ids(session)
    await notifications.push_many(
        session, user_ids=[u for u in staff if u != actor.id], type_=NOTIFY_NEW,
        title=f"Новая заявка №{ticket.number}/{ticket.year}: {ticket.title}",
        body=f"{CATEGORY_RU[enum_value(ticket.category)]}"
             f"{f' · {ticket.location}' if ticket.location else ''} · {actor.full_name}",
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
    )
    await audit.write_audit(
        session, actor=actor, action="TICKET_CREATE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "title": ticket.title,
                 "category": enum_value(ticket.category), "priority": enum_value(ticket.priority),
                 "location": ticket.location},
    )
    return ticket


async def update_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                        title=None, description=None, category=None, priority=None,
                        location=..., contact_phone=..., equipment_id=...) -> HelpdeskTicket:
    """Правка заявки.

    Автор — пока заявка не закрыта (уточнить формулировку, добавить кабинет).
    Системный администратор — в любом статусе: категорию и приоритет регулярно
    приходится исправлять уже по ходу работы.
    """
    staff = can_operate(actor)
    if not staff:
        if ticket.author_id != actor.id:
            raise forbidden("Изменять заявку может её автор или системный администратор")
        if ticket.status.value not in (TicketStatus.NEW.value, TicketStatus.IN_PROGRESS.value,
                                       TicketStatus.RESOLVED.value):
            raise conflict("Закрытую заявку изменить нельзя — создайте новую")

    changed: dict[str, str] = {}
    if title is not None and title.strip():
        changed["title"] = f"{ticket.title} → {title.strip()[:300]}"
        ticket.title = title.strip()[:300]
    if description is not None and description.strip():
        ticket.description = description.strip()
        changed["description"] = "уточнено описание"
    if category is not None and enum_value(category) != enum_value(ticket.category):
        changed["category"] = f"{CATEGORY_RU[enum_value(ticket.category)]} → {CATEGORY_RU[enum_value(category)]}"
        ticket.category = category
    if priority is not None and enum_value(priority) != enum_value(ticket.priority):
        changed["priority"] = f"{PRIORITY_RU[enum_value(ticket.priority)]} → {PRIORITY_RU[enum_value(priority)]}"
        ticket.priority = priority
        # норматив реакции пересчитываем, пока заявка не взята в работу:
        # после взятия он уже «отработан» и менять задним числом нельзя
        if ticket.taken_at is None:
            ticket.sla_hours = TICKET_SLA_HOURS.get(enum_value(priority))
            # норматив изменился — напоминание о старой просрочке уже неактуально,
            # разрешаем отправить новое (иначе повышение приоритета ничего бы не дало)
            ticket.sla_notified_at = None
    if location is not ...:
        ticket.location = _clean(location, 200)
        changed["location"] = ticket.location or "—"
    if contact_phone is not ...:
        ticket.contact_phone = _clean(contact_phone, 40)
        changed["contact_phone"] = ticket.contact_phone or "—"
    if equipment_id is not ...:
        if equipment_id is None:
            ticket.equipment_id = None
        else:
            from app.models.equipment import EquipmentItem

            if await session.get(EquipmentItem, equipment_id) is None:
                raise bad_request("Единица оборудования не найдена")
            ticket.equipment_id = equipment_id

    if not changed:
        raise bad_request("Нечего изменять")
    await _log_event(session, ticket, actor, "Изменена заявка: " + "; ".join(changed.values()))
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="TICKET_UPDATE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", **changed},
    )
    return ticket


# ------------------------------------------------------------- жизненный цикл
async def take_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                      assignee_id: uuid.UUID | None = None, comment: str | None = None) -> HelpdeskTicket:
    """Взять в работу (или переназначить другому системному администратору).

    Переназначение разрешено только внутри круга сисадминов: передать заявку
    учителю нельзя — иначе очередь превратилась бы в инструмент перекладывания.
    """
    if not can_operate(actor):
        raise forbidden("Брать заявку в работу может системный администратор")

    target = actor
    if assignee_id is not None and assignee_id != actor.id:
        candidate = await session.get(User, assignee_id)
        if candidate is None or candidate.is_deleted or not candidate.is_active:
            raise bad_request("Сотрудник не найден или недоступен")
        if not is_staff(candidate):
            raise bad_request("Назначить заявку можно только системному администратору")
        target = candidate

    reassign = ticket.assignee_id is not None and ticket.assignee_id != target.id
    if ticket.status == TicketStatus.IN_PROGRESS and not reassign:
        raise conflict("Заявка уже у вас в работе")
    _ensure_transition(ticket, TicketStatus.IN_PROGRESS)

    previous = ticket.assignee_id
    ticket.status = TicketStatus.IN_PROGRESS
    ticket.assignee_id = target.id
    ticket.rejected_reason = None
    if ticket.taken_at is None:
        ticket.taken_at = _now()
        reaction = _minutes(ticket.created_at, ticket.taken_at)
        # назначил другой человек — пишем «назначена», иначе в ленте значилось бы,
        # что исполнитель взял заявку сам (а он о ней мог ещё не знать)
        verb = (f"{target.full_name} взял(а) заявку в работу" if target.id == actor.id
                else f"Заявка назначена: {target.full_name}")
        await _log_event(session, ticket, actor,
                         f"{verb} (реакция: {reaction} мин, норматив {ticket.sla_hours or '—'} ч)")
    elif reassign:
        await _log_event(session, ticket, actor, f"Заявка передана: {target.full_name}")
    else:
        await _log_event(session, ticket, actor, f"Заявка снова в работе у {target.full_name}")
    if comment and comment.strip():
        session.add(HelpdeskMessage(ticket_id=ticket.id, author_id=actor.id,
                                    kind=TicketMessageKind.MESSAGE.value, body=comment.strip()[:4000]))
    await session.flush()

    # уведомляем автора и (при передаче) прежнего исполнителя
    recipients = {ticket.author_id, previous} - {None, actor.id}
    await notifications.push_many(
        session, user_ids=list(recipients), type_=NOTIFY_TAKEN,
        title=f"Заявка №{ticket.number}/{ticket.year} в работе",
        body=f"Исполнитель: {target.full_name}",
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
    )
    await audit.write_audit(
        session, actor=actor, action="TICKET_TAKE" if not reassign else "TICKET_REASSIGN",
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "assignee": target.full_name,
                 "reaction_minutes": _minutes(ticket.created_at, ticket.taken_at)},
    )
    return ticket


async def resolve_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                         resolution: str, comment: str | None = None) -> HelpdeskTicket:
    """Выполнено. Текст решения обязателен: по нему автор принимает работу."""
    if not can_operate(actor):
        raise forbidden("Закрывать заявку может системный администратор")
    if ticket.assignee_id is not None and ticket.assignee_id != actor.id:
        raise forbidden("Заявку выполняет другой системный администратор — сначала примите её")
    if not (resolution or "").strip():
        raise bad_request("Опишите, что сделано")
    _ensure_transition(ticket, TicketStatus.RESOLVED)

    ticket.status = TicketStatus.RESOLVED
    ticket.assignee_id = ticket.assignee_id or actor.id
    ticket.resolution = resolution.strip()
    ticket.resolved_at = _now()
    if ticket.taken_at is None:
        ticket.taken_at = ticket.resolved_at
    await _log_event(session, ticket, actor, f"Выполнено: {ticket.resolution}"[:4000])
    if comment and comment.strip():
        session.add(HelpdeskMessage(ticket_id=ticket.id, author_id=actor.id,
                                    kind=TicketMessageKind.MESSAGE.value, body=comment.strip()[:4000]))
    await session.flush()

    if ticket.author_id and ticket.author_id != actor.id:
        await notifications.push(
            session, user_id=ticket.author_id, type_=NOTIFY_RESOLVED,
            title=f"Заявка №{ticket.number}/{ticket.year} выполнена",
            body=f"{ticket.resolution[:300]}\n\nПодтвердите закрытие или верните на доработку.",
            entity_type=ENTITY_TYPE, entity_id=ticket.id,
        )
    await audit.write_audit(
        session, actor=actor, action="TICKET_RESOLVE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "resolution": ticket.resolution[:500],
                 "work_minutes": _minutes(ticket.taken_at, ticket.resolved_at)},
    )
    return ticket


async def close_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                       comment: str | None = None, auto: bool = False) -> HelpdeskTicket:
    """Автор подтвердил: работа принята, заявка закрыта."""
    if not auto and ticket.author_id != actor.id and not can_operate(actor):
        raise forbidden("Подтвердить выполнение может автор заявки или системный администратор")
    _ensure_transition(ticket, TicketStatus.CLOSED)

    ticket.status = TicketStatus.CLOSED
    ticket.closed_at = _now()
    ticket.closed_by_id = None if auto else actor.id
    ticket.auto_closed = auto
    if comment and comment.strip():
        session.add(HelpdeskMessage(ticket_id=ticket.id, author_id=None if auto else actor.id,
                                    kind=TicketMessageKind.MESSAGE.value, body=comment.strip()[:4000]))
    text = (f"Закрыта автоматически: автор не ответил {_autoclose_days()} дн."
            if auto else "Автор подтвердил выполнение — заявка закрыта")
    await _log_event(session, ticket, None if auto else actor, text)
    await session.flush()

    if not auto and ticket.assignee_id and ticket.assignee_id != actor.id:
        await notifications.push(
            session, user_id=ticket.assignee_id, type_=NOTIFY_CLOSED,
            title=f"Заявка №{ticket.number}/{ticket.year} закрыта автором",
            entity_type=ENTITY_TYPE, entity_id=ticket.id,
        )
    await audit.write_audit(
        session, actor=None if auto else actor, action="TICKET_CLOSE",
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "auto": auto},
    )
    return ticket


async def reopen_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                        comment: str) -> HelpdeskTicket:
    """Автор вернул выполненную заявку на доработку («не работает по-прежнему»)."""
    if ticket.author_id != actor.id:
        raise forbidden("Вернуть заявку на доработку может только её автор")
    if not (comment or "").strip():
        raise bad_request("Напишите, что осталось не сделано")
    _ensure_transition(ticket, TicketStatus.IN_PROGRESS)

    ticket.status = TicketStatus.IN_PROGRESS
    ticket.resolved_at = None
    ticket.resolution = None
    ticket.closed_at = None
    ticket.auto_closed = False
    session.add(HelpdeskMessage(ticket_id=ticket.id, author_id=actor.id,
                                kind=TicketMessageKind.MESSAGE.value, body=comment.strip()[:4000]))
    await _log_event(session, ticket, actor, "Автор вернул заявку на доработку")
    await session.flush()

    recipients = ({ticket.assignee_id} | set(await staff_ids(session))) - {None, actor.id}
    await notifications.push_many(
        session, user_ids=list(recipients), type_=NOTIFY_REOPENED,
        title=f"Заявка №{ticket.number}/{ticket.year} возвращена на доработку",
        body=comment.strip()[:300],
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
    )
    await audit.write_audit(
        session, actor=actor, action="TICKET_REOPEN", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "comment": comment.strip()[:300]},
    )
    return ticket


async def reject_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                        reason: str) -> HelpdeskTicket:
    """Отклонение: не наш профиль, дубликат, нужна служебная записка на закупку.

    Причина обязательна — автор должен понимать, куда идти дальше.
    """
    if not can_operate(actor):
        raise forbidden("Отклонять заявку может системный администратор")
    if not (reason or "").strip():
        raise bad_request("Укажите причину отклонения")
    _ensure_transition(ticket, TicketStatus.REJECTED)

    ticket.status = TicketStatus.REJECTED
    ticket.rejected_reason = reason.strip()[:1000]
    await _log_event(session, ticket, actor, f"Отклонено: {ticket.rejected_reason}")
    await session.flush()

    if ticket.author_id and ticket.author_id != actor.id:
        await notifications.push(
            session, user_id=ticket.author_id, type_=NOTIFY_REJECTED,
            title=f"Заявка №{ticket.number}/{ticket.year} отклонена",
            body=ticket.rejected_reason[:300],
            entity_type=ENTITY_TYPE, entity_id=ticket.id,
        )
    await audit.write_audit(
        session, actor=actor, action="TICKET_REJECT", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "reason": ticket.rejected_reason},
    )
    return ticket


async def cancel_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket, *,
                        comment: str | None = None) -> HelpdeskTicket:
    """Автор отозвал заявку: сам разобрался или подал ошибочно."""
    if ticket.author_id != actor.id and not can_operate(actor):
        raise forbidden("Отозвать заявку может её автор или системный администратор")
    _ensure_transition(ticket, TicketStatus.CANCELLED)

    ticket.status = TicketStatus.CANCELLED
    ticket.closed_at = _now()
    ticket.closed_by_id = actor.id
    await _log_event(session, ticket, actor,
                     (f"Отозвана: {comment.strip()}" if comment and comment.strip() else "Заявка отозвана"))
    await session.flush()

    if ticket.assignee_id and ticket.assignee_id != actor.id:
        await notifications.push(
            session, user_id=ticket.assignee_id, type_=NOTIFY_CANCELLED,
            title=f"Заявка №{ticket.number}/{ticket.year} отозвана автором",
            entity_type=ENTITY_TYPE, entity_id=ticket.id,
        )
    await audit.write_audit(
        session, actor=actor, action="TICKET_CANCEL", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "comment": (comment or "").strip()[:300]},
    )
    return ticket


async def delete_ticket(session: AsyncSession, actor: User, ticket: HelpdeskTicket) -> None:
    """Удаление заявки — только Владелец (журнал обращений — зона его ответственности)."""
    if actor.role != Role.OWNER:
        raise forbidden("Удалять заявки может только Владелец")
    label = f"{ticket.number}/{ticket.year}"
    files = list(ticket.files)
    await session.delete(ticket)
    await session.flush()

    from app.services import storage as _storage_mod

    for row in files:
        try:
            _storage_mod.storage.delete_pdf_pair(row.storage_path, row.pdf_path)
        except Exception:  # noqa: BLE001 — запись уже удалена, файл не критичен
            pass
    await audit.write_audit(
        session, actor=actor, action="TICKET_DELETE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": label, "title": ticket.title},
    )


# ------------------------------------------------------------------ переписка
async def add_message(session: AsyncSession, actor: User, ticket: HelpdeskTicket, body: str) -> HelpdeskMessage:
    """Реплика в ленте заявки. Писать могут автор и тот, кто её обслуживает."""
    text = (body or "").strip()
    if not text:
        raise bad_request("Пустое сообщение")
    if not (ticket.author_id == actor.id or can_operate(actor) or ticket.assignee_id == actor.id):
        raise forbidden("Переписка доступна автору заявки и системным администраторам")
    if ticket.status.value in (TicketStatus.CLOSED.value, TicketStatus.CANCELLED.value,
                               TicketStatus.REJECTED.value):
        raise conflict("Заявка закрыта — создайте новую, если проблема вернулась")

    row = HelpdeskMessage(ticket_id=ticket.id, author_id=actor.id,
                          kind=TicketMessageKind.MESSAGE.value, body=text[:4000])
    session.add(row)
    await session.flush()

    # адресат: автор пишет исполнителю, исполнитель — автору; если исполнителя
    # ещё нет, реплика уходит всей очереди сисадминов
    if ticket.author_id == actor.id:
        recipients = ({ticket.assignee_id} | (set(await staff_ids(session)) if ticket.assignee_id is None else set())) \
            - {None, actor.id}
    else:
        recipients = {ticket.author_id} - {None}
    await notifications.push_many(
        session, user_ids=list(recipients), type_=NOTIFY_MESSAGE,
        title=f"Заявка №{ticket.number}/{ticket.year}: {actor.full_name}",
        body=text[:300],
        entity_type=ENTITY_TYPE, entity_id=ticket.id,
    )
    await audit.write_audit(
        session, actor=actor, action="TICKET_MESSAGE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "chars": len(text)},
    )
    return row


async def delete_message(session: AsyncSession, actor: User, ticket: HelpdeskTicket,
                         message_id: uuid.UUID) -> None:
    """Удалить свою реплику (опечатка). Системные отметки не удаляются никому."""
    row = next((m for m in ticket.messages if m.id == message_id), None)
    if row is None:
        raise not_found("Сообщение")
    if enum_value(row.kind) == TicketMessageKind.SYSTEM.value:
        raise conflict("Служебные отметки удалить нельзя — это история заявки")
    if row.author_id != actor.id and actor.role != Role.OWNER:
        raise forbidden("Удалить можно только своё сообщение")
    await session.delete(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="TICKET_MESSAGE_DELETE", entity_type=ENTITY_TYPE,
        entity_id=ticket.id, changes={"number": f"{ticket.number}/{ticket.year}"},
    )


# ------------------------------------------------------------------- вложения
async def add_file(session: AsyncSession, actor: User, ticket: HelpdeskTicket, file) -> HelpdeskFile:
    """Фото поломки от автора или документ от администратора.

    PDF-копия здесь НЕ готовится синхронно: её ставит в фон хендлер
    (`document_pdf.ensure_pdf_background`), чтобы загрузка не ждала LibreOffice.
    """
    from app.config import settings
    from app.services import document_pdf
    from app.services import storage as _storage_mod

    staff = can_operate(actor)
    if ticket.author_id != actor.id and not staff:
        raise forbidden("Прикреплять файлы может автор заявки или системный администратор")
    if ticket.status.value not in (TicketStatus.NEW.value, TicketStatus.IN_PROGRESS.value,
                                   TicketStatus.RESOLVED.value):
        raise conflict("К закрытой заявке файлы не добавляются")

    mime = (file.content_type or "").lower().split(";")[0].strip()
    stored = await _storage_mod.storage.save(file.file, file.filename or "file.bin", mime,
                                             allowed_mimes=settings.allowed_task_file_mimes)
    row = HelpdeskFile(
        ticket_id=ticket.id, storage_path=stored.storage_path, original_name=stored.original_name,
        mime_type=stored.mime_type, size_bytes=stored.size_bytes, checksum=stored.checksum,
        kind=(TicketFileKind.STAFF.value if staff and ticket.author_id != actor.id
              else TicketFileKind.AUTHOR.value),
        uploaded_by_id=actor.id,
    )
    plan = document_pdf.plan_for(stored.mime_type)
    row.pdf_status = plan.status
    row.pdf_note = plan.note
    session.add(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="TICKET_FILE", entity_type=ENTITY_TYPE, entity_id=ticket.id,
        changes={"number": f"{ticket.number}/{ticket.year}", "file": row.original_name,
                 "size": row.size_bytes},
    )
    return row


async def delete_file(session: AsyncSession, actor: User, ticket: HelpdeskTicket,
                      file_id: uuid.UUID) -> None:
    row = next((f for f in ticket.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    if not (row.uploaded_by_id == actor.id or actor.role == Role.OWNER):
        raise forbidden("Удалить файл может загрузивший его сотрудник или Владелец")
    if ticket.status.value not in (TicketStatus.NEW.value, TicketStatus.IN_PROGRESS.value,
                                   TicketStatus.RESOLVED.value):
        raise conflict("Из закрытой заявки файлы не удаляются")

    from app.services import storage as _storage_mod

    _storage_mod.storage.delete_pdf_pair(row.storage_path, row.pdf_path)
    ticket.files.remove(row)
    await session.flush()
    await _log_event(session, ticket, actor, f"Удалён файл «{row.original_name}»")
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="TICKET_FILE_DELETE", entity_type=ENTITY_TYPE,
        entity_id=ticket.id, changes={"number": f"{ticket.number}/{ticket.year}",
                                      "file": row.original_name},
    )


# --------------------------------------------------------------------- списки
async def list_tickets(session: AsyncSession, actor: User, *, status: str | None = None,
                       category: str | None = None, priority: str | None = None,
                       assignee_id: uuid.UUID | None = None, author_id: uuid.UUID | None = None,
                       mine: bool = False, unassigned: bool = False, overdue: bool = False,
                       year: int | None = None, q: str | None = None,
                       limit: int = 200) -> list[HelpdeskTicket]:
    """Журнал заявок. Порядок: срочные и просроченные сверху, закрытые — по дате закрытия."""
    stmt = select(HelpdeskTicket)
    stmt = apply_visibility(stmt, actor)
    if status:
        stmt = stmt.where(HelpdeskTicket.status == status)
    if category:
        stmt = stmt.where(HelpdeskTicket.category == category)
    if priority:
        stmt = stmt.where(HelpdeskTicket.priority == priority)
    if mine:
        stmt = stmt.where(HelpdeskTicket.assignee_id == actor.id)
    if unassigned:
        stmt = stmt.where(HelpdeskTicket.assignee_id.is_(None))
    if assignee_id is not None:
        stmt = stmt.where(HelpdeskTicket.assignee_id == assignee_id)
    if author_id is not None:
        stmt = stmt.where(HelpdeskTicket.author_id == author_id)
    if year:
        stmt = stmt.where(HelpdeskTicket.year == year)
    if q:
        from app.dbutils import ilike

        stmt = stmt.where(or_(ilike(HelpdeskTicket.title, q),
                              ilike(HelpdeskTicket.description, q),
                              ilike(HelpdeskTicket.location, q)))
    if overdue:
        # просрочка реакции считается на Python (см. is_sla_breached) — фильтр
        # применяется к уже отобранной выборке ниже, поэтому здесь только «открытые»
        stmt = stmt.where(HelpdeskTicket.status == TicketStatus.NEW.value)

    stmt = stmt.order_by(HelpdeskTicket.year.desc(), HelpdeskTicket.number.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    if overdue:
        rows = [t for t in rows if is_sla_breached(t)]
    # открытые — первыми и по приоритету, закрытые — по моменту закрытия
    priority_rank = {TicketPriority.URGENT.value: 0, TicketPriority.HIGH.value: 1,
                     TicketPriority.NORMAL.value: 2, TicketPriority.LOW.value: 3}

    def sort_key(t: HelpdeskTicket):
        closed = t.status.value in (TicketStatus.CLOSED.value, TicketStatus.CANCELLED.value,
                                    TicketStatus.REJECTED.value)
        return (
            1 if closed else 0,
            0 if is_sla_breached(t) else 1,
            priority_rank.get(enum_value(t.priority), 9),
            -(t.number or 0),
        )

    rows.sort(key=sort_key)
    return rows


async def stats(session: AsyncSession, actor: User) -> dict:
    """Сводка для шапки раздела: сколько в очереди, у меня, просрочено."""
    rows = await list_tickets(session, actor, limit=1000)
    by_status: dict[str, int] = {}
    for t in rows:
        key = enum_value(t.status)
        by_status[key] = by_status.get(key, 0) + 1
    today = _now().date()
    reactions = [m for t in rows if (m := _minutes(t.created_at, t.taken_at)) is not None]
    reactions.sort()
    return {
        "total": len(rows),
        "new": by_status.get(TicketStatus.NEW.value, 0),
        "in_progress": by_status.get(TicketStatus.IN_PROGRESS.value, 0),
        "resolved": by_status.get(TicketStatus.RESOLVED.value, 0),
        "closed": by_status.get(TicketStatus.CLOSED.value, 0),
        "rejected": by_status.get(TicketStatus.REJECTED.value, 0),
        "cancelled": by_status.get(TicketStatus.CANCELLED.value, 0),
        "mine": sum(1 for t in rows if t.assignee_id == actor.id
                    and t.status.value == TicketStatus.IN_PROGRESS.value),
        "sla_breached": sum(1 for t in rows if is_sla_breached(t)),
        "resolved_today": sum(1 for t in rows if t.resolved_at and _aware(t.resolved_at).date() == today),
        "median_reaction_minutes": reactions[len(reactions) // 2] if reactions else None,
        "is_staff": can_view_all(actor),
    }


# ---------------------------------------------------------------- housekeeping
def _autoclose_days() -> int:
    from app.config import settings

    return int(getattr(settings, "ticket_autoclose_days", 5) or 5)


async def autoclose_resolved(session: AsyncSession) -> int:
    """Автозакрытие RESOLVED-заявок, которые автор так и не подтвердил.

    Вызывается из housekeeping-цикла. Автор молчит — работа принята: иначе в
    очереди вечно висели бы «выполненные», а статистика времени ремонта портилась.
    """
    from app.enums import TICKET_AUTOCLOSE_DAYS

    days = _autoclose_days() or TICKET_AUTOCLOSE_DAYS
    cutoff = _now() - timedelta(days=days)
    rows = (await session.execute(
        select(HelpdeskTicket).where(HelpdeskTicket.status == TicketStatus.RESOLVED.value,
                                     HelpdeskTicket.resolved_at.is_not(None))
    )).scalars().all()
    closed = 0
    for ticket in rows:
        if _aware(ticket.resolved_at) and _aware(ticket.resolved_at) < cutoff:
            await close_ticket(session, None, ticket, auto=True)   # актора нет: закрывает система
            closed += 1
    if closed:
        await session.flush()
    return closed


async def notify_sla_breach(session: AsyncSession) -> int:
    """Напоминание сисадминам о заявках с просроченной реакцией (раз в цикл).

    Пишет уведомление не чаще раза в сутки на заявку: флаг хранится в payload
    последней отметки, поэтому отдельной таблицы не нужно.
    """
    rows = (await session.execute(
        select(HelpdeskTicket).where(
            HelpdeskTicket.status == TicketStatus.NEW.value,
            HelpdeskTicket.sla_notified_at.is_(None),
        )
    )).scalars().all()
    breached = [t for t in rows if is_sla_breached(t)]
    if not breached:
        return 0
    staff = await staff_ids(session)
    if not staff:
        return 0
    titles = ", ".join(f"№{t.number}/{t.year}" for t in breached[:8])
    await notifications.push_many(
        session, user_ids=staff, type_=NOTIFY_SLA,
        title=f"Просрочена реакция по заявкам: {titles}",
        body=f"Всего {len(breached)} шт. Возьмите в работу или отклоните с указанием причины.",
        entity_type=ENTITY_TYPE, entity_id=breached[0].id,
    )
    # помечаем, что по заявке уже напомнили: напоминание ОДНО, иначе каждый цикл
    # housekeeping присылал бы сисадмину одно и то же сообщение
    moment = _now()
    for ticket in breached:
        ticket.sla_notified_at = moment
        await _log_event(session, ticket, None,
                         "Норматив реакции истёк — системным администраторам отправлено напоминание")
    await session.flush()
    await audit.write_audit(
        session, actor=None, action="TICKET_SLA_BREACH", entity_type=ENTITY_TYPE,
        changes={"tickets": [f"{t.number}/{t.year}" for t in breached[:20]], "count": len(breached)},
    )
    return len(breached)
