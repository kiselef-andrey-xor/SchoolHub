"""Автоматические напоминания об ознакомлении с приказами.

Фоновый цикл внутри контейнера backend:
  * каждый час — персональные in-app напоминания сотрудникам, у которых приказ
    не ознакомлен, а срок истекает через `reminder_days_before` дней или сегодня;
  * ежедневно не раньше `reminder_hour` (по умолчанию 9:00) — сводка администрации
    «Просроченные ознакомления» (кто, какой приказ, сколько дней просрочки).

Дедупликация: не более одного напоминания по паре (сотрудник, приказ) в сутки
и не более одной сводки на администратора в сутки. Внешние каналы (Max/e-mail)
не подключены: in-app уведомления покрывают потребность без внешних зависимостей.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.enums import AckStatus, OrderStatus, Role
from app.models.notification import Notification
from app.models.order import Order, OrderAcknowledgement
from app.models.user import User
from app.services import notifications

logger = logging.getLogger("schoolhub.reminders")


def _day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


async def remind_ack_deadlines(session) -> int:
    """Персональные напоминания: срок истекает скоро или сегодня."""
    today = date.today()
    horizon = today + timedelta(days=max(0, settings.reminder_days_before))
    rows = (
        await session.execute(
            select(Order, OrderAcknowledgement, User)
            .join(OrderAcknowledgement, OrderAcknowledgement.order_id == Order.id)
            .join(User, User.id == OrderAcknowledgement.user_id)
            .where(
                Order.status == OrderStatus.PUBLISHED,
                Order.ack_deadline.is_not(None),
                Order.ack_deadline <= horizon,
                OrderAcknowledgement.status == AckStatus.PENDING,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
            )
        )
    ).all()
    sent = 0
    for order, ack, user in rows:
        days = (order.ack_deadline - today).days
        dup = (
            await session.execute(
                select(Notification.id).where(
                    Notification.user_id == user.id,
                    Notification.type == notifications.ORDER_ACK_DEADLINE,
                    Notification.entity_id == order.id,
                    Notification.created_at >= _day_start(today),
                ).limit(1)
            )
        ).first()
        if dup:
            continue
        title = (
            f"Приказ № {order.number}: сегодня последний день ознакомления"
            if days <= 0
            else f"Приказ № {order.number}: до конца ознакомления {days} дн."
        )
        await notifications.push(
            session,
            user_id=user.id,
            type_=notifications.ORDER_ACK_DEADLINE,
            title=title,
            body=order.title,
            entity_type="order",
            entity_id=order.id,
            payload={"deadline": order.ack_deadline.isoformat(), "days_left": days},
        )
        sent += 1
    return sent


async def overdue_digest(session) -> int:
    """Ежедневная сводка администрации по просроченным ознакомлениям."""
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
        )
    ).all()
    if not rows:
        return 0
    admins = (
        await session.execute(
            select(User).where(User.role.in_([Role.OWNER, Role.ADMIN]), User.is_active.is_(True))
        )
    ).scalars().all()
    lines = []
    for order, _ack, user in rows[:20]:
        days = (today - order.ack_deadline).days
        lines.append(f"— приказ № {order.number} «{order.title[:60]}»: {user.full_name}, просрочка {days} дн.")
    body = "\n".join(lines) + (f"\n… и ещё {len(rows) - 20}" if len(rows) > 20 else "")
    sent = 0
    for admin in admins:
        dup = (
            await session.execute(
                select(Notification.id).where(
                    Notification.user_id == admin.id,
                    Notification.type == notifications.ACK_OVERDUE_DIGEST,
                    Notification.created_at >= _day_start(today),
                ).limit(1)
            )
        ).first()
        if dup:
            continue
        await notifications.push(
            session,
            user_id=admin.id,
            type_=notifications.ACK_OVERDUE_DIGEST,
            title=f"Просроченные ознакомления: {len(rows)}",
            body=body,
        )
        sent += 1
    return sent


# ------------------------------------------------- аттестация и КПК: сроки
async def _expiring_certs(session):
    """Записи об аттестации/КПК с истёкшим или приближающимся сроком."""
    from app.models.certification import Certification

    horizon = date.today() + timedelta(days=max(0, settings.cert_reminder_days))
    return (
        await session.execute(
            select(Certification, User)
            .join(User, User.id == Certification.user_id)
            .where(
                Certification.expires_at.is_not(None),
                Certification.expires_at <= horizon,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
            )
            .order_by(Certification.expires_at)
        )
    ).all()


def _cert_label(cert) -> str:
    from app.models.certification import CertKind

    if cert.kind == CertKind.CPK:
        return f"курс «{(cert.title or '').strip()[:60]}»"
    return f"категория «{(cert.category or '').strip()[:40]}»"


async def remind_certifications(session) -> int:
    """Персональные напоминания: срок категории/КПК истекает или уже истёк."""
    rows = await _expiring_certs(session)
    today = date.today()
    sent = 0
    for cert, user in rows:
        days = (cert.expires_at - today).days
        dup = (
            await session.execute(
                select(Notification.id).where(
                    Notification.user_id == user.id,
                    Notification.type == notifications.CERT_EXPIRING,
                    Notification.entity_id == cert.id,
                    Notification.created_at >= _day_start(today),
                ).limit(1)
            )
        ).first()
        if dup:
            continue
        title = (
            f"Срок {_cert_label(cert)} истёк {days * -1} дн. назад — нужна переаттестация"
            if days < 0
            else f"{_cert_label(cert).capitalize()}: срок истекает через {days} дн."
        )
        await notifications.push(
            session,
            user_id=user.id,
            type_=notifications.CERT_EXPIRING,
            title=title,
            body=cert.provider or cert.document_no or "",
            entity_type="certification",
            entity_id=cert.id,
            payload={"expires_at": cert.expires_at.isoformat(), "days_left": days},
        )
        sent += 1
    return sent


async def cert_digest(session) -> int:
    """Ежедневная сводка администрации по приближающимся/прошедшим срокам аттестации."""
    rows = await _expiring_certs(session)
    if not rows:
        return 0
    today = date.today()
    admins = (
        await session.execute(
            select(User).where(User.role.in_([Role.OWNER, Role.ADMIN]), User.is_active.is_(True))
        )
    ).scalars().all()
    lines = []
    for cert, user in rows[:20]:
        days = (cert.expires_at - today).days
        when = f"истёк {-days} дн. назад" if days < 0 else f"осталось {days} дн."
        lines.append(f"— {user.full_name}: {_cert_label(cert)}, {when}")
    body = "\n".join(lines) + (f"\n… и ещё {len(rows) - 20}" if len(rows) > 20 else "")
    sent = 0
    for admin in admins:
        dup = (
            await session.execute(
                select(Notification.id).where(
                    Notification.user_id == admin.id,
                    Notification.type == notifications.CERT_DIGEST,
                    Notification.created_at >= _day_start(today),
                ).limit(1)
            )
        ).first()
        if dup:
            continue
        await notifications.push(
            session,
            user_id=admin.id,
            type_=notifications.CERT_DIGEST,
            title=f"Аттестация и КПК: сроки у {len(rows)} записей",
            body=body,
        )
        sent += 1
    return sent


async def _loop() -> None:
    await asyncio.sleep(20)                       # даём приложению стартовать
    while True:
        try:
            async with SessionLocal() as session:
                # повторяющиеся задачи: выполненные в прошлых периодах «воскресают»
                # (ежечасно, поэтому к утру ежедневные задачи снова в работе)
                from app.services import tasks as tasks_service

                reset = await tasks_service.rollover_due(session)
                if reset:
                    logger.info("reminders: повторяющихся задач возвращено в работу: %s", reset)

                n = await remind_ack_deadlines(session)
                n += await remind_certifications(session)
                now = datetime.now()
                if now.hour >= settings.reminder_hour:
                    await overdue_digest(session)
                    await cert_digest(session)

                # self-healing: автоочистка истёкших сессий и раздутого журнала событий
                from app.services import housekeeping

                await housekeeping.housekeeping_cycle(session)
                await session.commit()
                if n:
                    logger.info("reminders: отправлено напоминаний: %s", n)
        except asyncio.CancelledError:
            raise
        except Exception:                          # фоновый цикл не должен ронять приложение
            logger.exception("reminders: ошибка цикла")
        await asyncio.sleep(3600)


def start_reminders() -> asyncio.Task | None:
    if not settings.reminders_enabled:
        logger.info("reminders: отключены (REMINDERS_ENABLED=false)")
        return None
    return asyncio.create_task(_loop())
