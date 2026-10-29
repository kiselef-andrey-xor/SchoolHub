"""Схемы журнала входящих писем."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserOut


class IncomingCreateIn(BaseModel):
    """Ручная запись журнала (авто-записи создаются из задач)."""

    incoming_number: str = Field(min_length=1, max_length=120)
    incoming_date: date | None = None            # по умолчанию — сегодня
    sent_date: date | None = None
    addressee: str | None = Field(default=None, max_length=255)
    subject: str = Field(min_length=3, max_length=500)
    note: str | None = Field(default=None, max_length=1000)
    responsible_id: uuid.UUID | None = None


class IncomingUpdateIn(BaseModel):
    reg_no: int | None = Field(default=None, ge=1, le=100000, description="№ регистрации вручную")
    incoming_number: str | None = Field(default=None, min_length=1, max_length=120)
    incoming_date: date | None = None
    sent_date: date | None = None
    addressee: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, min_length=3, max_length=500)
    note: str | None = Field(default=None, max_length=1000)
    responsible_id: uuid.UUID | None = None


class IncomingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reg_year: int
    reg_no: int
    incoming_date: date
    incoming_number: str
    sent_date: date | None = None
    addressee: str | None = None
    subject: str
    note: str | None = None
    responsible: UserOut | None = None
    task_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
