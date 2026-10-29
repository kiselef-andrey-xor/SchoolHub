"""Схемы справочника должностей."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    aliases: str = ""
    sort_order: int = 0


class PositionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    aliases: str = Field(default="", max_length=1000)


class PositionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=255)
    aliases: str | None = Field(default=None, max_length=1000)
    sort_order: int | None = None
