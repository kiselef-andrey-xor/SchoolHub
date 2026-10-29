"""Учёт оборудования и инвентаря: простой справочник с количественным учётом."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.types import GUID


class EquipmentItem(Base):
    __tablename__ = "equipment_items"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)             # наименование
    inventory_number: Mapped[str | None] = mapped_column(String(120), index=True)  # инвентарный номер
    serial_number: Mapped[str | None] = mapped_column(String(120))            # заводской (серийный) номер
    barcode: Mapped[str | None] = mapped_column(String(128), index=True)      # штрихкод (EAN-13/Code-128) для сканирования
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # количество
    location: Mapped[str | None] = mapped_column(String(255))                 # местонахождение
    note: Mapped[str | None] = mapped_column(String(1000))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EquipmentMove(Base):
    """Перемещение позиции инвентаря (из кабинета в кабинет и т.п.)."""

    __tablename__ = "equipment_moves"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_location: Mapped[str | None] = mapped_column(String(255))
    to_location: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    move_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(1000))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
