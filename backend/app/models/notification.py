"""Уведомления (in-app). Источник событий — приказы и задачи."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base
from app.types import GUID


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)

    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # ORDER_PUBLISHED | ORDER_ACK_DEADLINE | TASK_ASSIGNED | TASK_STATUS_CHANGED | TASK_COMMENT
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(String(1000))

    entity_type: Mapped[str | None] = mapped_column(String(30))      # order | task
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"))

    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
