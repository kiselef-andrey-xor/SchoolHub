"""Self-healing и автоочистка (фоновый «домовёнок», крутится в цикле reminders).

  1. Автоочистка сессий: из таблицы refresh_sessions удаляются токены, срок действия
     которых истёк более `session_purge_after_days` (30) дней назад. Таблица не
     разрастается до миллионов строк за годы работы школы.
  2. Self-healing журнала: если audit_log раздувается сверх `audit_archive_threshold`,
     самые старые строки выгружаются в JSONL-архив на диске (storage/audit_archive/)
     и удаляются из БД — журнал перестаёт тормозить запросы. Владелец получает
     уведомление с именем файла архива.

Обе операции идемпотентны и дёшевы (счетчики + индексные DELETE), поэтому выполняются
каждый час; уведомление отправляется только когда ФАКТИЧЕСКИ была работа.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import SessionLocal
from app.enums import Role
from app.models.audit import AuditLog
from app.models.session import RefreshSession
from app.models.user import User
from app.services import notifications
from app.services.files_backup import write_audit_archive

logger = logging.getLogger("schoolhub.housekeeping")


async def purge_expired_sessions(session) -> int:
    """Удаляет refresh-токены, истёкшие глубже grace-периода (по умолчанию месяц)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, settings.session_purge_after_days))
    result = await session.execute(delete(RefreshSession).where(RefreshSession.expires_at < cutoff))
    return result.rowcount or 0


async def archive_bloated_audit(session) -> dict | None:
    """Если журнал раздут — архивирует старое в JSONL и чистит таблицу."""
    total = (await session.execute(select(func.count()).select_from(AuditLog))).scalar() or 0
    if total <= settings.audit_archive_threshold:
        return None
    excess = total - settings.audit_keep_rows
    if excess <= 0:
        return None
    rows = (
        await session.execute(
            select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(excess)
        )
    ).scalars().all()
    if not rows:
        return None
    payload = [
        {
            "id": str(r.id),
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "actor_email": r.actor_email,
            "actor_name": r.actor_name,
            "actor_role": r.actor_role,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": str(r.entity_id) if r.entity_id else None,
            "changes": r.changes,
            "ip_address": r.ip_address,
            "user_agent": r.user_agent,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    path = write_audit_archive(payload)
    ids = [r.id for r in rows]
    await session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
    return {"archived": len(ids), "file": path.name, "total_before": total, "kept": settings.audit_keep_rows}


async def helpdesk_maintenance(session) -> dict:
    """Обслуживание очереди заявок Helpdesk (v1.12).

    Автозакрытие: автор молчит `ticket_autoclose_days` дней после выполнения —
    работа считается принятой. Иначе в очереди вечно висели бы «выполненные»,
    а медиана времени ремонта портилась незакрытыми заявками.

    Напоминание о просрочке реакции отправляется ОДИН раз на заявку
    (`sla_notified_at`), поэтому частый цикл housekeeping не засыпает
    сисадмина одинаковыми уведомлениями.
    """
    from app.services import helpdesk

    closed = await helpdesk.autoclose_resolved(session)
    breaches = await helpdesk.notify_sla_breach(session)
    return {"tickets_autoclosed": closed, "sla_breaches": breaches}


async def run_housekeeping(session) -> dict:
    """Один проход: сессии + журнал + Helpdesk. Возвращает отчёт (пустой = работы не было)."""
    report: dict = {"sessions_purged": 0, "audit": None, "helpdesk": None}
    report["sessions_purged"] = await purge_expired_sessions(session)
    report["audit"] = await archive_bloated_audit(session)
    report["helpdesk"] = await helpdesk_maintenance(session)
    return report


async def housekeeping_cycle(session=None) -> None:
    """Точка входа фонового цикла: своя транзакция + уведомление Владельца.

    session передаёт фоновый цикл reminders, чтобы не плодить соединения;
    без session — открывается собственное (ручной запуск, тесты)."""
    if not settings.housekeeping_enabled:
        return

    async def _run(s) -> None:
        report = await run_housekeeping(s)
        purged = report["sessions_purged"]
        audit = report["audit"]
        helpdesk_report = report.get("helpdesk") or {}
        # автозакрытие заявок уведомлением Владельцу не сопровождается: оно уже
        # отправило отметку в ленту каждой заявки, а сводка здесь только для журнала
        if not purged and not audit:
            return
        owners = (
            await s.execute(
                select(User).where(User.role == Role.OWNER, User.is_deleted.is_(False))
            )
        ).scalars().all()
        parts = []
        if purged:
            parts.append(f"удалено истёкших сессий: {purged}")
        if audit:
            parts.append(
                f"журнал событий раздут ({audit['total_before']} строк): в архив {audit['file']} "
                f"выгружено {audit['archived']} старых записей, в БД оставлено {audit['kept']}"
            )
        body = "Автоочистка: " + "; ".join(parts) + ". Подробности: Профиль → «Резервное копирование»."
        for owner in owners:
            await notifications.push(
                s,
                user_id=owner.id,
                type_=notifications.HOUSEKEEPING,
                title="Автоочистка базы данных",
                body=body,
            )
        if helpdesk_report.get("tickets_autoclosed"):
            logger.info("housekeeping: автозакрыто заявок Helpdesk: %s",
                        helpdesk_report["tickets_autoclosed"])
        logger.info("housekeeping: %s", body)

    if session is not None:
        await _run(session)
        return
    async with SessionLocal() as own:
        await _run(own)
        await own.commit()
