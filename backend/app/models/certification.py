"""Модуль «Аттестация и КПК».

Учёт сроков действия квалификационных категорий и курсов повышения квалификации
(КПК) по каждому сотруднику:
  * kind = CATEGORY — квалификационная категория (высшая, первая, …):
    важны `category`, дата присвоения (awarded_at) и срок действия (expires_at);
  * kind = CPK — курс повышения квалификации: важны название (title),
    количество часов (hours), учебное заведение (provider) и дата окончания.

`expires_at` может быть пустым — тогда запись считается бессрочной (например,
высшая категория по действующим правилам устанавливается бессрочно).
Напоминания о приближающихся/прошедших сроках — в app/services/reminders.py.
Скан документа (удостоверение/приказ) хранится в файловом хранилище (file).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import User
from app.types import GUID


class CertKind:
    CATEGORY = "CATEGORY"   # квалификационная категория
    CPK = "CPK"             # курс повышения квалификации

    ALL = (CATEGORY, CPK)


# подсказки для формы/импорта — единые написания категорий
CATEGORY_PRESETS = (
    "Высшая",
    "Первая",
    "Соответствие занимаемой должности",
    "Без категории",
)


class Certification(Base):
    __tablename__ = "certifications"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default=CertKind.CATEGORY, index=True)

    # --- категория (kind = CATEGORY) ---
    category: Mapped[str | None] = mapped_column(String(120))

    # --- курс КПК (kind = CPK) ---
    title: Mapped[str | None] = mapped_column(String(500))    # название курса/программы
    hours: Mapped[int | None] = mapped_column(Integer)        # объём в часах
    provider: Mapped[str | None] = mapped_column(String(300))  # учебное заведение / ИРО

    # --- общие ---
    document_no: Mapped[str | None] = mapped_column(String(200))   # № удостоверения / приказа
    awarded_at: Mapped[date | None] = mapped_column(Date)          # дата присвоения / окончания курса
    expires_at: Mapped[date | None] = mapped_column(Date, index=True)  # действует до; NULL = бессрочно
    file: Mapped[str | None] = mapped_column(String(512))          # скан в файловом хранилище
    file_name: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(String(1000))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # две FK на users в одной модели — обеим отношениям нужен явный foreign_keys
    user: Mapped["User"] = relationship(lazy="joined", foreign_keys=[user_id])
    created_by: Mapped["User | None"] = relationship(lazy="joined", foreign_keys=[created_by_id])
