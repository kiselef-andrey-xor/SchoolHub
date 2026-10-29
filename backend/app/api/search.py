"""Глобальный поиск (Ctrl+K в шапке): приказы, задачи, входящие письма, люди.

Каждая секция отдаётся только тем, кому она видна в обычных списках:
  * приказы   — администрация/Владелец: все, учителя: только опубликованные;
  * задачи    — администрация/Владелец: все, остальные: где сотрудник исполнитель;
  * входящие  — только при гранте журнала входящих (как сам журнал);
  * люди      — только администрация/Владелец;
  * ВШК       — опубликованный план годового контроля (черновик — только администрации).
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import String, cast, or_, select

from app.dbutils import escape_like, ilike
from app.enums import OrderStatus, PlanStatus, Role
from app.models.equipment import EquipmentItem
from app.models.incoming import IncomingLetter
from app.models.movement import MovementOrder
from app.models.order import Order
from app.models.task import Task, TaskAssignee
from app.models.user import User
from app.security import CurrentUser, SessionDep
from app.services import lockdown as lockdown_mod

router = APIRouter(prefix="/search", tags=["search"])

LIMIT = 6  # результатов на секцию — достаточно для выпадающего списка


def _item(row_id, title: str, subtitle: str, url: str) -> dict:
    return {"id": str(row_id), "title": title, "subtitle": subtitle, "url": url}


@router.get("", summary="Глобальный поиск по разделам системы")
async def global_search(
    session: SessionDep,
    actor: CurrentUser,
    q: str = Query(min_length=2, max_length=120),
):
    term = q.strip()
    # `ilike()` принимает «сырой» текст: сам экранирует `%`/`_` и оборачивает шаблон.
    # Для двух «ручных» .ilike() ниже нужен уже собранный шаблон — потому raw_like.
    raw_like = f"%{escape_like(term)}%"
    is_staff = actor.role in (Role.OWNER, Role.ADMIN)
    out: dict[str, list[dict]] = {
        "orders": [], "tasks": [], "incoming": [], "users": [],
        "vshk": [], "certifications": [], "equipment": [], "movement": [],
        "notes": [], "help": [], "helpdesk": [],
    }

    # --- приказы: по номеру и названию ---
    conds = [or_(ilike(Order.title, term), ilike(Order.number, term))]
    if not is_staff:
        conds.append(Order.status == OrderStatus.PUBLISHED.value)
    rows = (
        await session.execute(select(Order).where(*conds).order_by(Order.created_at.desc()).limit(LIMIT))
    ).scalars().all()
    out["orders"] = [
        _item(o.id, f"Приказ №{o.number} ({o.order_year})", o.title[:120], f"/orders/{o.id}") for o in rows
    ]

    # --- задачи: по названию и входящему номеру ---
    stmt = select(Task)
    conds = [or_(ilike(Task.title, term), ilike(Task.incoming_number, term))]
    if not is_staff:
        stmt = stmt.join(TaskAssignee, TaskAssignee.task_id == Task.id).where(TaskAssignee.user_id == actor.id)
    rows = (
        await session.execute(stmt.where(*conds).order_by(Task.created_at.desc()).limit(LIMIT))
    ).scalars().all()
    out["tasks"] = [
        _item(
            t.id,
            t.title,
            f"до {t.deadline:%d.%m.%Y}" if t.deadline else "без дедлайна",
            f"/tasks/{t.id}",
        )
        for t in rows
    ]

    # --- журнал входящих: только при гранте; ищем и № регистрации тоже ---
    if is_staff or actor.incoming_journal_access:
        reg_combo = (
            cast(IncomingLetter.reg_no, String) + "/" + cast(IncomingLetter.reg_year, String)
        )
        rows = (
            await session.execute(
                select(IncomingLetter)
                .where(
                    or_(
                        ilike(IncomingLetter.subject, term),
                        ilike(IncomingLetter.incoming_number, term),
                        cast(IncomingLetter.reg_no, String).ilike(raw_like, escape="\\"),
                        reg_combo.ilike(raw_like, escape="\\"),
                    )
                )
                .order_by(IncomingLetter.reg_year.desc(), IncomingLetter.reg_no.desc())
                .limit(LIMIT)
            )
        ).scalars().all()
        out["incoming"] = [
            _item(
                r.id,
                f"№ рег. {r.reg_no}/{r.reg_year} · {r.incoming_number}",
                r.subject[:120],
                f"/incoming?search={r.incoming_number}",
            )
            for r in rows
        ]

    # --- люди: только staff ---
    if is_staff:
        rows = (
            await session.execute(
                select(User)
                .where(User.is_deleted.is_(False), ilike(User.full_name, term))
                .order_by(User.full_name)
                .limit(LIMIT)
            )
        ).scalars().all()
        from app.services import lockdown

        out["users"] = [
            _item(
                u.id,
                lockdown.mask_employee(str(u.id)) if lockdown.masking() else u.full_name,
                ", ".join(p.title for p in (u.positions or [])),
                f"/users?search={'' if lockdown.masking() else u.full_name}",
            )
            for u in rows
        ]

    # --- ВШК: мероприятия годового плана по цели/направлению ---
    from app.models.vshk import VshkItem, VshkPlan

    stmt = (
        select(VshkItem, VshkPlan.year_label)
        .join(VshkPlan, VshkPlan.id == VshkItem.plan_id)
        .where(or_(ilike(VshkItem.title, term), ilike(VshkItem.section, term)))
    )
    if not is_staff:
        stmt = stmt.where(VshkPlan.status == PlanStatus.PUBLISHED.value)
    rows = (await session.execute(stmt.order_by(VshkItem.sort_order).limit(LIMIT))).all()
    out["vshk"] = [
        _item(it.id, it.title[:120], f"ВШК {year}: {it.section or 'мероприятие'}", f"/vshk?item={it.id}")
        for it, year in rows
    ]

    # --- аттестация и КПК: администрация — все, сотрудник — только свои ---
    from app.models.certification import Certification

    stmt = select(Certification).join(User, User.id == Certification.user_id).where(
        or_(
            ilike(Certification.category, term),
            ilike(Certification.title, term),
            ilike(Certification.provider, term),
            ilike(Certification.document_no, term),
            ilike(User.full_name, term),
        )
    )
    if not is_staff:
        stmt = stmt.where(Certification.user_id == actor.id)
    rows = (await session.execute(stmt.order_by(Certification.expires_at.asc().nulls_last()).limit(LIMIT))).scalars().all()
    out["certifications"] = [
        _item(
            c.id,
            (c.title or c.category or "Запись")[:120],
            f"{'КПК' if c.kind == 'CPK' else 'Категория'} · {c.user.full_name if c.user else ''}"
            + (f" · до {c.expires_at:%d.%m.%Y}" if c.expires_at else " · бессрочно"),
            "/certifications",
        )
        for c in rows
    ]

    # --- инвентарь: наименование, инвентарный/серийный номер, штрихкод ---
    if is_staff or actor.equipment_access:
        rows = (
            await session.execute(
                select(EquipmentItem)
                .where(
                    or_(
                        ilike(EquipmentItem.name, term),
                        ilike(EquipmentItem.inventory_number, term),
                        ilike(EquipmentItem.serial_number, term),
                        ilike(EquipmentItem.barcode, term),
                    )
                )
                .order_by(EquipmentItem.name)
                .limit(LIMIT)
            )
        ).scalars().all()
        out["equipment"] = [
            _item(
                e.id,
                e.name[:120],
                " · ".join(
                    x for x in (
                        f"инв. {e.inventory_number}" if e.inventory_number else None,
                        e.location,
                    ) if x
                ),
                f"/equipment?search={e.inventory_number or e.barcode or e.name}",
            )
            for e in rows
        ]

    # --- движение обучающихся: ФИО ребёнка и номер приказа ---
    if is_staff or actor.movement_access:
        rows = (
            await session.execute(
                select(MovementOrder)
                .where(or_(ilike(MovementOrder.student, term), ilike(MovementOrder.order_number, term)))
                .order_by(MovementOrder.order_date.desc())
                .limit(LIMIT)
            )
        ).scalars().all()
        out["movement"] = [
            _item(
                m.id,
                f"{lockdown_mod.mask_student(m.student) if lockdown_mod.masking() else m.student} — {m.order_number}",
                f"{'ЗАЧИСЛЕНИЕ' if m.order_type == 'ENROLLMENT' else 'ПЕРЕВОД' if m.order_type == 'TRANSFER' else 'ОТЧИСЛЕНИЕ'} от {m.order_date:%d.%m.%Y}",
                f"/movement?search={m.order_number}",
            )
            for m in rows
        ]

    # --- служебные записки: свои — автору/исполнителю/согласующему, все — Владельцу и гранту ---
    from app.models.service_note import ServiceNote
    from app.services.service_notes import apply_visibility

    stmt = select(ServiceNote).where(
        or_(ilike(ServiceNote.subject, term), ilike(ServiceNote.number, term))
    )
    stmt = apply_visibility(stmt, actor)
    rows = (await session.execute(stmt.order_by(ServiceNote.created_at.desc()).limit(LIMIT))).scalars().all()
    out["notes"] = [
        _item(n.id, n.subject[:120],
              f"Служебная записка №{n.number}/{n.year}" if n.number else "Служебная записка (черновик)",
              f"/notes/{n.id}")
        for n in rows
    ]

    # --- справка и инструкции: в пределах видимости роли (ALL / ADMIN / OWNER) ---
    from app.models.help import HelpArticle
    from app.services.help import visible_to

    rows = (
        await session.execute(
            select(HelpArticle)
            .where(HelpArticle.visibility.in_(visible_to(actor)),
                   or_(ilike(HelpArticle.title, term), ilike(HelpArticle.category, term)))
            .order_by(HelpArticle.category, HelpArticle.title)
            .limit(LIMIT)
        )
    ).scalars().all()
    out["help"] = [
        _item(a.id, a.title[:120], f"Справка · {a.category}", f"/help?article={a.id}") for a in rows
    ]

    # --- заявки Helpdesk: свои — автору, вся очередь — сисадминам и администрации ---
    from app.models.helpdesk import HelpdeskTicket
    from app.services import helpdesk as helpdesk_service

    # видимость та же, что у журнала заявок: учитель находит только свои обращения
    stmt = helpdesk_service.apply_visibility(
        select(HelpdeskTicket).where(
            or_(ilike(HelpdeskTicket.title, term), ilike(HelpdeskTicket.description, term))
        ),
        actor,
    )
    rows = (
        await session.execute(stmt.order_by(HelpdeskTicket.created_at.desc()).limit(LIMIT))
    ).scalars().all()
    out["helpdesk"] = [
        _item(
            t.id, t.title[:120],
            f"Заявка №{t.number}/{t.year} · {helpdesk_service.status_label(t.status)}"
            if t.number else "Заявка в техподдержку",
            f"/helpdesk?t={t.id}",
        )
        for t in rows
    ]

    return out
