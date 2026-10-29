"""Сессии (refresh-токены): одна строка на каждый выданный refresh-токен.

Модель «семейств» (token family):
  * вход создаёт семейство (family_id) и первый токен (jti);
  * обновление РОТИРУЕТ токен: старый помечается superseded_at, в то же семейство
    добавляется новый jti;
  * предъявление уже заменённого (superseded) токена вне короткого льготного окна —
    признак кражи: семейство отзывается целиком (reuse_detected);
  * выход, смена/сброс пароля и блокировка учётки отзывают токены
    (revoked_at), поэтому «жить ещё 14 дней после выхода» они больше не могут.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import User
from app.types import GUID


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # ротация: токен заменён новым в том же семействе
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # явный отзыв (выход, смена пароля, блокировка, reuse_detected)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revocation_reason: Mapped[str | None] = mapped_column(String(60))

    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # «запомнить меня»
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped["User"] = relationship(lazy="joined")
