"""Справочник должностей-ролей (ВШК, план работы).

Должность — назначаемая администрацией роль сотрудника («зам. директора по УВР»,
«классный руководитель», «руководитель ШМО»…). У пользователя может быть несколько
должностей одновременно (связь многие-ко-многим через user_positions).

Должности используются как адресаты мероприятий: в плане работы и в плане ВШК
ответственным может быть сотрудник, должность целиком или «Администрация» —
тогда мероприятие попадает на дашборды всех держателей должности (или всех
сотрудников с ролью OWNER/ADMIN для «Администрации»).

Поле aliases — синонимы для распознавания должностей в импортируемых Word-документах
(«зам по УВР», «руководители ШМО» и т.п.).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.types import GUID


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # синонимы через запятую — для распознавания в импортируемых документах
    aliases: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserPosition(Base):
    __tablename__ = "user_positions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("positions.id", ondelete="CASCADE"), primary_key=True
    )
