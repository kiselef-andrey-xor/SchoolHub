"""Общие схемы: пагинация, справочники, healthcheck."""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=200)
    pages: int


class HealthOut(BaseModel):
    status: str = "ok"
    app: str
    env: str
    version: str


class EnumItem(BaseModel):
    value: str
    label: str


class DirectoryItem(BaseModel):
    """Элемент справочника для селектов на фронте (роли, типы приказов, приоритеты…)."""

    value: str
    label: str
    group: str | None = None
