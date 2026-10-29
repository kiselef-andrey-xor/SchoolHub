"""Динамические типы приказов: справочник, редактируемый администрацией."""
from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrderTypeRow(Base):
    __tablename__ = "order_types"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)   # HR, STUDENTS, CUSTOM_…
    title: Mapped[str] = mapped_column(String(120), nullable=False)  # «По личному составу»
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
