"""Схемы модуля ВШК (внутришкольный контроль)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import PlanStatus, VshkStatus
from app.schemas.plan import _check_resp_refs
from app.schemas.user import UserOut


class VshkPlanCreateIn(BaseModel):
    year_start: int | None = Field(default=None, ge=2000, le=2100)


class VshkItemIn(BaseModel):
    month: int = Field(ge=1, le=12)
    section: str = Field(default="", max_length=500)
    title: str = Field(min_length=3, max_length=1000)
    control_type: str = Field(default="", max_length=255)
    resp: list[str] = Field(default_factory=list, max_length=40)
    summary: str = Field(default="", max_length=500)
    status: VshkStatus = VshkStatus.PLANNED

    @field_validator("resp")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_resp_refs(v)

    @field_validator("section", "title", "control_type", "summary")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


class VshkItemUpdate(BaseModel):
    month: int | None = Field(default=None, ge=1, le=12)
    section: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, min_length=3, max_length=1000)
    control_type: str | None = Field(default=None, max_length=255)
    resp: list[str] | None = Field(default=None, max_length=40)
    summary: str | None = Field(default=None, max_length=500)
    status: VshkStatus | None = None

    @field_validator("resp")
    @classmethod
    def _refs(cls, v: list[str] | None) -> list[str] | None:
        return _check_resp_refs(v) if v is not None else None


class VshkResultIn(BaseModel):
    result_text: str | None = Field(default=None, max_length=2000)
    clear: bool = False     # снять результат (вернуть статус без итога)


class VshkItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    month: int
    section: str = ""
    title: str
    control_type: str = ""
    resp: list[str] = []
    resp_titles: list[str] = []      # человекочитаемые ответственные (сервер раскрывает ссылки)
    summary: str = ""
    status: VshkStatus
    result_text: str | None = None
    result_file: str | None = None
    result_file_name: str | None = None
    result_at: datetime | None = None
    sort_order: int = 0


class VshkPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    year_start: int
    year_label: str
    status: PlanStatus
    published_at: datetime | None = None
    created_by: UserOut | None = None
    can_manage: bool = False
    items: list[VshkItemOut] = []


class VshkPlanListOut(BaseModel):
    id: uuid.UUID
    year_start: int
    year_label: str
    status: PlanStatus
    items_total: int = 0
    published_at: datetime | None = None


class VshkImportOut(BaseModel):
    plan_id: uuid.UUID
    year_label: str
    items_created: int
    sections: int
    warnings: list[str] = []         # нераспознанные ответственные и замечания разбора
