"""Схемы раздела «Движение обучающихся»."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

TYPE_LABELS = {"TRANSFER": "перевод", "ENROLLMENT": "зачисление", "EXPULSION": "отчисление"}


class MovementChildIn(BaseModel):
    student: str = Field(min_length=2, max_length=255)
    birth_date: date | None = None
    from_place: str | None = Field(default=None, max_length=255)
    to_place: str | None = Field(default=None, max_length=255)


class MovementChildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    student: str
    birth_date: date | None = None
    from_place: str | None = None
    to_place: str | None = None

    @field_serializer("student", "birth_date")
    def _mask_child(self, v):
        from app.services import lockdown

        if not lockdown.masking():
            return v
        return lockdown.mask_student(v) if isinstance(v, str) else None


class MovementIn(BaseModel):
    order_type: str = Field(description="TRANSFER | ENROLLMENT | EXPULSION")
    guardian: str | None = Field(default=None, max_length=2000,
                                 description="Законный представитель(и); необязательно")
    student: str | None = Field(default=None, max_length=255,
                                description="Обучающийся (если детей несколько — используйте children)")
    birth_date: date | None = None
    children: list[MovementChildIn] = Field(default_factory=list, max_length=500)
    from_place: str | None = Field(default=None, max_length=255, description="Откуда (по умолчанию для всех детей)")
    to_place: str | None = Field(default=None, max_length=255, description="Куда (по умолчанию для всех детей)")
    order_date: date
    order_number: str = Field(min_length=1, max_length=120)
    signatory: str | None = Field(default=None, max_length=255, description="Подписант (директор)")
    note: str | None = Field(default=None, max_length=1000)
    status: str = "NEW"

    @model_validator(mode="after")
    def _normalize(self):
        if self.order_type not in TYPE_LABELS:
            raise ValueError("Неизвестный тип приказа о движении")
        if self.status not in ("NEW", "DONE", "ERROR"):
            raise ValueError("Неизвестный статус")
        kids = list(self.children)
        if self.student and self.student.strip():
            kids.insert(0, MovementChildIn(student=self.student.strip(), birth_date=self.birth_date,
                                           from_place=self.from_place, to_place=self.to_place))
        if not kids:
            raise ValueError("Добавьте хотя бы одного обучающегося")
        self.children = kids
        for kid in self.children:
            if self.order_type in ("TRANSFER", "EXPULSION") and not (kid.from_place or "").strip() and not (self.from_place or "").strip():
                raise ValueError("Для перевода и отчисления укажите «откуда»")
            if self.order_type in ("TRANSFER", "ENROLLMENT") and not (kid.to_place or "").strip() and not (self.to_place or "").strip():
                raise ValueError("Для перевода и зачисления укажите «куда»")
        return self


class MovementUpdateIn(BaseModel):
    order_type: str | None = None
    guardian: str | None = Field(default=None, max_length=2000)
    children: list[MovementChildIn] | None = Field(default=None, max_length=500)
    from_place: str | None = Field(default=None, max_length=255)
    to_place: str | None = Field(default=None, max_length=255)
    order_date: date | None = None
    order_number: str | None = Field(default=None, min_length=1, max_length=120)
    signatory: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)
    status: str | None = None


class MovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_type: str
    status: str
    guardian: str | None = None
    children: list[MovementChildOut] = []
    from_place: str | None = None
    to_place: str | None = None
    order_date: date
    order_number: str
    signatory: str | None = None
    note: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("guardian")
    def _mask_guardian(self, v: str | None):
        from app.services import lockdown

        return lockdown.mask_guardian(v) if (lockdown.masking() and v) else v

    @field_serializer("signatory")
    def _mask_signatory(self, v: str | None):
        from app.services import lockdown

        return lockdown.mask_employee(v) if (lockdown.masking() and v) else v
