"""Модуль «План работы на неделю».

План — электронный аналог школьного Word-документа «План работы ДД.ММ–ДД.ММ»:
дежурный класс, блоки «в течение месяца» / «в течение недели» и мероприятия
по дням (пн–сб) с ответственными из числа сотрудников.

Содержимое хранится единым JSON-документом (content): план — небольшой
структурированный документ, который всегда читается и пишется ЦЕЛИКОМ,
поэтому нормализация по таблицам только усложнила бы код, не давая выигрыша.
Ответственные (resp) — ссылки: UUID сотрудника, «pos:<uuid>» (должность целиком)
или «admin» (вся администрация); при публикации все адресаты получают уведомление.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import Date, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base
from app.enums import PlanStatus
from app.models.user import User
from app.types import GUID, EnumString


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    # Неделя плана: понедельник (уникален — один план на неделю) … суббота.
    week_start: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    week_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[PlanStatus] = mapped_column(
        EnumString(PlanStatus, 20), nullable=False, default=PlanStatus.DRAFT, index=True
    )

    # JSON-документ: {duty: {klass, teacher_id}, notes: [...], month_items: [...],
    #                 week_items: [...], days: [{date, events: [...]}]}
    content: Mapped[dict] = mapped_column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped["User | None"] = relationship(lazy="joined")

    # --- derived ---
    @staticmethod
    def compute_week_end(week_start: date) -> date:
        """Суббота той же недели (пн + 5 дней)."""
        return week_start + timedelta(days=5)

    def iter_items(self):
        """Все пункты и мероприятия плана: (kind, item) — для сбора ответственных."""
        content = self.content or {}
        for it in content.get("month_items") or []:
            yield "month", it
        for it in content.get("week_items") or []:
            yield "week", it
        for day in content.get("days") or []:
            for ev in day.get("events") or []:
                yield "event", ev

    def resp_refs(self) -> list[str]:
        """Уникальные ссылки ответственных (строки): «<uuid>» | «pos:<uuid>» | «admin».

       _plain uuid_ — конкретный сотрудник; _pos:_ — должность целиком (все держатели);
        _admin_ — вся администрация (роли OWNER/ADMIN). Дежурный класс тоже учитывается.
        """
        seen: dict[str, None] = {}
        duty = (self.content or {}).get("duty") or {}
        if duty.get("teacher_id"):
            seen[str(duty["teacher_id"])] = None
        for _, item in self.iter_items():
            for ref in item.get("resp") or []:
                seen[str(ref)] = None
        return list(seen.keys())

    def responsible_ids(self) -> list[str]:
        """UUID сотрудников, указанных напрямую (обратная совместимость уведомлений)."""
        out: list[str] = []
        for ref in self.resp_refs():
            if not ref.startswith(("pos:", "admin", "text:")):
                out.append(ref)
        return out

    def events_total(self) -> int:
        return sum(1 for kind, _ in self.iter_items() if kind == "event")

    def items_for_keys(self, keys: set[str]) -> int:
        """Сколько пунктов/мероприятий плана адресованы владельцу набора ссылок keys."""
        n = 0
        duty = (self.content or {}).get("duty") or {}
        if duty.get("teacher_id") and str(duty["teacher_id"]) in keys:
            n += 1
        for _, item in self.iter_items():
            if any(str(x) in keys for x in (item.get("resp") or [])):
                n += 1
        return n
