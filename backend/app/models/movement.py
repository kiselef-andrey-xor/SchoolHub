"""Приказы по движению обучающихся (перевод / зачисление / отчисление).

Раздел для секретаря: ввод реквизитов по шаблону бумажного приказа, печать
готового документа и сводка для внесения данных в внешние системы.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.types import GUID


class MovementOrder(Base):
    __tablename__ = "movement_orders"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    order_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)   # TRANSFER | ENROLLMENT | EXPULSION
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NEW", index=True)  # NEW | DONE | ERROR

    guardian: Mapped[str | None] = mapped_column(String(2000))   # законные представители (может быть несколько через "; ")
    student: Mapped[str] = mapped_column(String(255), nullable=False)       # обучающийся (ФИО полностью)
    birth_date: Mapped[date | None] = mapped_column(Date)                    # дата рождения первого ребёнка (денорм.)
    from_place: Mapped[str | None] = mapped_column(String(255))             # откуда (перевод/отчисление)
    to_place: Mapped[str | None] = mapped_column(String(255))               # куда (перевод/зачисление)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)          # дата приказа
    order_number: Mapped[str] = mapped_column(String(120), nullable=False)  # номер приказа
    signatory: Mapped[str | None] = mapped_column(String(255))              # кто подписывает (ФИО директора)
    note: Mapped[str | None] = mapped_column(String(1000))                  # примечание (причина и т.п.)
    print_text: Mapped[str | None] = mapped_column(String(8000))            # своя печатная форма (тело приказа)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MovementChild(Base):
    """Обучающийся в приказе по движению (в приказе может быть много детей)."""

    __tablename__ = "movement_children"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    movement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("movement_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student: Mapped[str] = mapped_column(String(255), nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date)
    from_place: Mapped[str | None] = mapped_column(String(255))
    to_place: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(default=0, nullable=False)   # порядок в таблице приказа
