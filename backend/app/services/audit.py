"""Запись событий аудита. Вызывается из сервисов, не из роутеров (единая точка)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User
from app.types import enum_value

# Фабрика сессий для «отвязанных» записей аудита. В тестах подменяется на тестовую
# (иначе запись шла бы в боевую БД из settings). См. tests/conftest.py::app
session_factory_override = None


def _meta(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    from app.security import client_ip

    ua = request.headers.get("user-agent")
    return client_ip(request), (ua[:512] if ua else None)


async def write_audit(
    session: AsyncSession,
    *,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | str | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> AuditLog:
    ip, ua = _meta(request)
    log = AuditLog(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        actor_name=actor.full_name if actor else None,
        actor_role=enum_value(actor.role) if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=uuid.UUID(str(entity_id)) if entity_id else None,
        changes=changes,
        ip_address=ip,
        user_agent=ua,
    )
    session.add(log)
    await session.flush()
    return log


async def write_audit_detached(
    *,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | str | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Пишет событие в ОТДЕЛЬНОЙ транзакции и коммитит её сразу.

    Нужно для записей, которые должны пережить откат основного запроса —
    прежде всего `LOGIN_FAILED`: без этого журнал терял бы все неудачные попытки
    входа (а именно они важны для расследования и для контроля блокировок AD).
    """
    from app.database import SessionLocal

    factory = session_factory_override or SessionLocal
    try:
        async with factory() as session:
            await write_audit(
                session,
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                changes=changes,
                request=request,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — журнал не должен ломать основной сценарий
        import logging

        logging.getLogger("schoolhub.audit").exception("Не удалось записать событие %s", action)


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Компактный diff для аудита: {field: {from, to}} только по изменившимся полям.

    Значения приводятся к строке, отсутствующие (None) — опускаются, чтобы журнал
    читался человеком: {"position": {"to": "Учитель информатики"}}.
    """
    out: dict[str, Any] = {}
    for key, new in after.items():
        old = before.get(key)
        if str(old) == str(new):
            continue
        entry: dict[str, str] = {}
        if old is not None:
            entry["from"] = str(old)
        if new is not None:
            entry["to"] = str(new)
        out[key] = entry
    return out
