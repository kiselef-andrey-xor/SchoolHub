"""Закрепляемые виды списков: пользователь настраивает фильтры списка и нажимает
«Запомнить этот вид» — параметры сохраняются по ключу списка (orders, tasks, …)
и применяются при следующем входе с любого устройства."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base
from app.types import GUID


class UserListView(Base):
    __tablename__ = "user_list_views"
    __table_args__ = (UniqueConstraint("user_id", "list_key", name="uq_user_list_view"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    list_key: Mapped[str] = mapped_column(String(40), nullable=False)   # orders | tasks | incoming | …
    params: Mapped[dict[str, Any]] = mapped_column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
