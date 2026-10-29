"""Модуль «Приказы»: документ, его файлы-версии и подтверждения ознакомления."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base
from app.enums import AckStatus, OrderStatus
from app.models.user import User
from app.types import GUID, EnumString, enum_value


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("number", "order_year", name="uq_orders_number_year"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    order_type: Mapped[str] = mapped_column(
        String(40), nullable=False, default="ADMIN", index=True
    )  # код из справочника order_types (динамические типы приказов)
    status: Mapped[OrderStatus] = mapped_column(
        EnumString(OrderStatus, 20), nullable=False, default=OrderStatus.DRAFT, index=True
    )
    issued_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Крайний срок ознакомления — используется для напоминаний и отчётов.
    ack_deadline: Mapped[date | None] = mapped_column(Date)
    require_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Выбранный круг ознакомления (список UUID-строк) — используется при require_all=False.
    # Задаётся ещё в черновике и может быть изменён после публикации (PUT /orders/{id}/recipients).
    recipient_ids: Mapped[list | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"))

    # --- ПЭП: требуемый уровень подписи ознакомления (см. app.enums.SignaturePolicy) ---
    # NONE — без подписи (аудит), CLICK — клик «Ознакомлен» (текущее), TOTP — код, CODE_WORD — код+слово.
    # Для типа STUDENTS действует автоправило: политика не ниже TOTP (app.services.totp.effective_policy).
    signature_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="CLICK", server_default="CLICK"
    )

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- relations ---
    files: Mapped[list["OrderFile"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderFile.version.desc()",
        lazy="selectin",
    )
    acknowledgements: Mapped[list["OrderAcknowledgement"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    created_by: Mapped["User | None"] = relationship(lazy="joined")

    # --- derived ---
    @property
    def current_file(self) -> "OrderFile | None":
        return self.files[0] if self.files else None

    @property
    def ack_total(self) -> int:
        return len(self.acknowledgements)

    @property
    def ack_done(self) -> int:
        return sum(1 for a in self.acknowledgements if enum_value(a.status) == AckStatus.ACKNOWLEDGED.value)


class OrderFile(Base):
    """Неизменяемая версия файла приказа. Загрузка нового файла = новая версия (аудит-след)."""

    __tablename__ = "order_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))         # sha256 — контроль целостности
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="files")
    uploaded_by: Mapped["User | None"] = relationship(lazy="joined")


class OrderAcknowledgement(Base):
    """Лист ознакомления: одна строка на (приказ × сотрудник).

    Создаётся при публикации приказа для всех активных учителей (require_all)
    либо для явно выбранного списка. Подтверждение фиксирует время, IP и UA.
    """

    __tablename__ = "order_acknowledgements"
    __table_args__ = (UniqueConstraint("order_id", "user_id", name="uq_ack_order_user"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[AckStatus] = mapped_column(
        EnumString(AckStatus, 20), nullable=False, default=AckStatus.PENDING, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Фактический способ подписи (NONE/CLICK/TOTP/CODE_WORD) — печатается в листе ознакомления
    sign_method: Mapped[str | None] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="acknowledgements")
    user: Mapped["User"] = relationship(lazy="joined")
