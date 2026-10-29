"""Журнал входящих писем: записи создаются автоматически из задач,
у которых указан «входящий номер», либо вручную владельцем/сотрудником с грантом.

Колонки журнала (бизнес-требование):
  № регистрации, дата входящего, № входящего, дата отправки (вручную),
  адресат, тема письма, примечание, ответственный.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.types import GUID


class IncomingLetter(Base):
    __tablename__ = "incoming_letters"
    __table_args__ = (UniqueConstraint("reg_year", "reg_no", name="uq_incoming_reg"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    reg_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reg_no: Mapped[int] = mapped_column(Integer, nullable=False)              # № регистрации за год
    incoming_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # дата входящего
    incoming_number: Mapped[str] = mapped_column(String(120), nullable=False)      # № входящего
    sent_date: Mapped[date | None] = mapped_column(Date)                      # дата отправки (вручную)
    addressee: Mapped[str | None] = mapped_column(String(255))                # адресат
    subject: Mapped[str] = mapped_column(String(500), nullable=False)         # тема письма
    note: Mapped[str | None] = mapped_column(String(1000))                    # примечание
    responsible_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    responsible: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[responsible_id], lazy="selectin"
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )  # связь с задачей-источником (None для ручных записей)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
