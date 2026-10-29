"""Схемы модуля «Приказы»."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import AckStatus, OrderStatus, SignaturePolicy
from app.schemas.user import UserOut


class OrderFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    version: int
    is_current: bool
    checksum: str | None = None
    created_at: datetime
    uploaded_by: UserOut | None = None


class AcknowledgementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: AckStatus
    acknowledged_at: datetime | None = None
    sign_method: str | None = None      # NONE/CLICK/TOTP/CODE_WORD — способ подписи (ПЭП)
    comment: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    user: UserOut


class OrderBase(BaseModel):
    number: str = Field(min_length=1, max_length=64, examples=["112-к"])
    order_year: int = Field(ge=2000, le=2200)
    title: str = Field(min_length=3, max_length=500)
    description: str | None = None
    order_type: str = "ADMIN"
    issued_on: date
    ack_deadline: date | None = None
    require_all: bool = True
    # выбранный круг ознакомления (при require_all=False) — задаётся ещё в черновике
    recipient_ids: list[uuid.UUID] | None = None
    # ПЭП: требуемый уровень подписи; для типа STUDENTS автоправило поднимает до TOTP
    signature_policy: SignaturePolicy = SignaturePolicy.CLICK


class OrderCreateIn(OrderBase):
    pass


class OrderUpdateIn(BaseModel):
    number: str | None = Field(default=None, min_length=1, max_length=64)
    order_year: int | None = Field(default=None, ge=2000, le=2200)
    title: str | None = Field(default=None, min_length=3, max_length=500)
    description: str | None = None
    order_type: str | None = None
    issued_on: date | None = None
    ack_deadline: date | None = None
    require_all: bool | None = None
    recipient_ids: list[uuid.UUID] | None = None
    signature_policy: SignaturePolicy | None = None
    status: OrderStatus | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    order_year: int
    title: str
    description: str | None
    order_type: str
    status: OrderStatus
    issued_on: date
    ack_deadline: date | None
    require_all: bool
    recipient_ids: list[str] = []
    signature_policy: str = "CLICK"          # объявленная политика приказа
    effective_policy: str = "CLICK"          # действующая (с учётом автоправила типа)
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    created_by: UserOut | None = None

    files: list[OrderFileOut] = []
    current_file: OrderFileOut | None = None

    # --- производные поля (считаются в сервисе) ---
    ack_total: int = 0
    ack_done: int = 0
    my_ack: AcknowledgementOut | None = None
    can_manage: bool = False


class OrderListOut(BaseModel):
    """Компактное представление для списка (без полного листа ознакомления)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    order_year: int
    title: str
    order_type: str
    status: OrderStatus
    issued_on: date
    ack_deadline: date | None
    signature_policy: str = "CLICK"
    effective_policy: str = "CLICK"
    published_at: datetime | None
    created_by: UserOut | None = None
    current_file: OrderFileOut | None = None
    ack_total: int = 0
    ack_done: int = 0
    my_ack: AcknowledgementOut | None = None
    can_manage: bool = False


class AckCreateIn(BaseModel):
    comment: str | None = Field(default=None, max_length=500)
    # ПЭП: одноразовый код аутентификатора (для политик TOTP/CODE_WORD)
    code: str | None = Field(default=None, max_length=10)
    # контрольное слово (только для политики CODE_WORD)
    word: str | None = Field(default=None, max_length=120)


class AckListIn(BaseModel):
    """Назначение круга ознакомления (вместо require_all)."""

    user_ids: list[uuid.UUID] = Field(min_length=1)
