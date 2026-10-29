from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_email: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    action: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    changes: dict[str, Any] | None = None
    ip_address: str | None = None
    created_at: datetime

    # «Локдаун»: журнал тоже обезличиваем (кто именно — видно только Владельцу)
    @field_serializer("actor_name")
    def _mask_actor(self, v: str | None) -> str | None:
        from app.services import lockdown

        if not lockdown.masking():
            return v
        return lockdown.mask_employee(str(self.actor_id)) if self.actor_id else v

    @field_serializer("actor_email", "ip_address")
    def _mask_meta(self, v: str | None):
        from app.services import lockdown

        return None if lockdown.masking() else v

    # `changes` хранит снимки полей (ФИО, e-mail, телефон, обучающийся…), поэтому
    # обезличивается отдельно: без этого журнал раскрывал ПДн при включённом локдауне
    # (review/REVIEW.md §P1-1). Маскируем на ЧТЕНИИ — содержимое БД не портим.
    @field_serializer("changes")
    def _mask_changes(self, v: dict[str, Any] | None) -> dict[str, Any] | None:
        from app.services import lockdown

        return lockdown.mask_changes(v)

    # человекопонятное представление (см. app.services.audit_human)
    summary: str = ""
    lines: list[str] = []
