"""Сквозной аудит-лог значимых действий (требование для школ: кто/что/когда)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base
from app.types import GUID


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(320))
    actor_name: Mapped[str | None] = mapped_column(String(255))   # ФИО на момент действия
    actor_role: Mapped[str | None] = mapped_column(String(20))

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    # LOGIN | ORDER_CREATE | ORDER_UPDATE | ORDER_PUBLISH | ORDER_FILE_UPLOAD | ORDER_ACK
    # TASK_CREATE | TASK_UPDATE | TASK_STATUS | USER_CREATE | USER_UPDATE ...

    entity_type: Mapped[str | None] = mapped_column(String(30), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)

    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
