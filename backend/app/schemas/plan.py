"""Схемы модуля «План работы на неделю».

Content — структурированный JSON-документ плана (зеркалит Word-макет):
  duty        — дежурный класс и его классный руководитель;
  notes       — дополнительные строки шапки («В столовой дежурят 7-ые классы»);
  month_items — блок «В течение месяца»;
  week_items  — блок «В течение недели»;
  days        — мероприятия по дням (пн–сб): дата + события (время, место, ответственные).
Ответственные (resp) — список UUID активных сотрудников.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import PlanStatus
from app.schemas.user import UserOut


class PlanDutyIn(BaseModel):
    klass: str | None = Field(default=None, max_length=50)
    teacher_id: uuid.UUID | None = None


def _check_resp_refs(refs: list[str]) -> list[str]:
    out: list[str] = []
    for ref in refs:
        ref = str(ref).strip()
        if ref == "admin":
            value = ref
        elif ref.startswith("pos:"):
            uuid.UUID(ref[4:])
            value = ref
        else:
            uuid.UUID(ref)
            value = ref
        if value not in out:
            out.append(value)
    return out


class PlanItemIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    place: str | None = Field(default=None, max_length=200)
    resp: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Текст пункта не может быть пустым")
        return v

    @field_validator("resp")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_resp_refs(v)


class PlanEventIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    time: str | None = Field(default=None, max_length=60)
    place: str | None = Field(default=None, max_length=200)
    resp: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название мероприятия не может быть пустым")
        return v

    @field_validator("resp")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_resp_refs(v)


class PlanDayIn(BaseModel):
    date: date
    events: list[PlanEventIn] = Field(default_factory=list, max_length=40)


class PlanContentIn(BaseModel):
    duty: PlanDutyIn = Field(default_factory=PlanDutyIn)
    notes: list[str] = Field(default_factory=list, max_length=10)
    month_items: list[PlanItemIn] = Field(default_factory=list, max_length=30)
    week_items: list[PlanItemIn] = Field(default_factory=list, max_length=40)
    days: list[PlanDayIn] = Field(default_factory=list, max_length=7)

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, v: list[str]) -> list[str]:
        return [x.strip()[:300] for x in v if x and x.strip()]


class PlanIn(BaseModel):
    week_start: date
    content: PlanContentIn = Field(default_factory=PlanContentIn)

    @field_validator("week_start")
    @classmethod
    def _monday(cls, v: date) -> date:
        if v.weekday() != 0:
            raise ValueError("Неделя плана должна начинаться с понедельника")
        return v


class PlanUpdateIn(BaseModel):
    week_start: date | None = None
    content: PlanContentIn | None = None

    @field_validator("week_start")
    @classmethod
    def _monday(cls, v: date | None) -> date | None:
        if v is not None and v.weekday() != 0:
            raise ValueError("Неделя плана должна начинаться с понедельника")
        return v


# --------------------------------------------------------------------- ответы
class PlanDutyOut(BaseModel):
    klass: str | None = None
    teacher: UserOut | None = None


class RespRefOut(BaseModel):
    """Ответственный в виде ссылки: сотрудник / должность / администрация."""

    kind: str                    # user | pos | admin
    id: str | None = None        # uuid сотрудника или должности
    title: str                   # «Кузнецова О.В.» / «Классный руководитель» / «Администрация»


class PlanItemOut(BaseModel):
    text: str
    place: str | None = None
    resp: list[RespRefOut] = []


class PlanEventOut(BaseModel):
    title: str
    time: str | None = None
    place: str | None = None
    resp: list[RespRefOut] = []


class PlanDayOut(BaseModel):
    date: date
    weekday: str                # «понедельник»… — заполняется на сервере
    events: list[PlanEventOut] = []


class PlanContentOut(BaseModel):
    duty: PlanDutyOut = Field(default_factory=PlanDutyOut)
    notes: list[str] = []
    month_items: list[PlanItemOut] = []
    week_items: list[PlanItemOut] = []
    days: list[PlanDayOut] = []


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    week_start: date
    week_end: date
    status: PlanStatus
    content: PlanContentOut
    created_by: UserOut | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # производные — для списков и бейджей
    events_total: int = 0
    responsibles_total: int = 0
    my_items: int = 0
    can_manage: bool = False


class PlanListOut(BaseModel):
    """Компактный элемент списка планов (без содержимого)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    week_start: date
    week_end: date
    status: PlanStatus
    duty_class: str | None = None
    events_total: int = 0
    responsibles_total: int = 0
    my_items: int = 0
    published_at: datetime | None = None
    created_by: UserOut | None = None
    can_manage: bool = False
