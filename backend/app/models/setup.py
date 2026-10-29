"""Состояние первоначальной настройки (одна строка в таблице)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.types import GUID


class SetupState(Base):
    __tablename__ = "setup_state"

    SINGLETON_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=lambda: SetupState.SINGLETON_ID)

    school_name: Mapped[str | None] = mapped_column(String(200))
    # глобальный шаблон печатной формы приказов по движению (плейсхолдеры подставляются при печати)
    movement_template: Mapped[str | None] = mapped_column(Text)
    # одноразовый код, который показывается в журнале контейнера и требуется
    # для создания первого Владельца через веб-интерфейс
    setup_token: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
