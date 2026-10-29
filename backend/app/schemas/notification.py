from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str | None = None

    # Заголовок и текст уведомления — системная строка, собранная в момент события
    # («Иванов И. И. отметил(а) задачу…»), поэтому имена в ней не помечены как поля.
    # В режиме «Локдаун» заменяем известные ФИО сотрудников их метками — иначе
    # колокольчик и всплывающие тосты раскрывали ПДн в обход всех сериализаторов
    # (review/REVIEW.md §P1-1). В БД текст остаётся исходным.
    @field_serializer("title", "body")
    def _mask_names(self, v: str | None) -> str | None:
        from app.services import lockdown

        return lockdown.scrub(v)
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None
    is_read: bool
    created_at: datetime
