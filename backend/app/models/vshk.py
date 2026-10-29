"""Модуль ВШК — внутришкольный контроль (годовой план).

План ВШК — электронный аналог школьного Word-документа «План внутришкольного
контроля на учебный год»: мероприятия сгруппированы по месяцам (август–июнь)
и направлениям контроля; у каждого мероприятия — цель, вид контроля,
ответственные (сотрудники / должности / «Администрация»), срок и форма
подведения итогов, статус выполнения и результат (текст + файл справки).

План импортируется из DOCX (таблица «Цель | Вид | Ответственный | Срок и форма»)
или ведётся вручную; черновик виден только администрации, после публикации
план виден всем, а ответственные получают уведомление и блок «Мой ВШК»
на дашборде за текущий месяц.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base
from app.enums import PlanStatus, VshkStatus
from app.models.user import User
from app.types import GUID, EnumString


def academic_year_start(d) -> int:
    """Год начала учебного года: август–декабрь → тот же год, январь–июль → предыдущий."""
    return d.year if d.month >= 8 else d.year - 1


MONTHS_RU = {
    8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ",
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ", 5: "МАЙ", 6: "ИЮНЬ",
}
# порядок месяцев учебного года в документе
MONTH_ORDER = [8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6]


class VshkPlan(Base):
    __tablename__ = "vshk_plans"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    year_start: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)  # 2026 → 2026-2027
    year_label: Mapped[str] = mapped_column(String(20), nullable=False)                          # «2026-2027»
    status: Mapped[PlanStatus] = mapped_column(
        EnumString(PlanStatus, 20), nullable=False, default=PlanStatus.DRAFT, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["VshkItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="VshkItem.sort_order"
    )
    created_by: Mapped["User | None"] = relationship(lazy="joined")


class VshkItem(Base):
    __tablename__ = "vshk_items"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("vshk_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)   # 1–12
    section: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # направление контроля
    title: Mapped[str] = mapped_column(String(1000), nullable=False)           # цель контроля
    control_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # вид контроля
    # ответственные: ссылки вида "<uuid>" (сотрудник), "pos:<uuid>" (должность), "admin"
    resp: Mapped[list[Any]] = mapped_column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=list)
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # срок и форма итогов
    status: Mapped[VshkStatus] = mapped_column(
        EnumString(VshkStatus, 20), nullable=False, default=VshkStatus.PLANNED, index=True
    )
    result_text: Mapped[str | None] = mapped_column(String(2000))
    result_file: Mapped[str | None] = mapped_column(String(512))   # путь в файловом хранилище
    result_file_name: Mapped[str | None] = mapped_column(String(255))
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    plan: Mapped["VshkPlan"] = relationship(back_populates="items")
