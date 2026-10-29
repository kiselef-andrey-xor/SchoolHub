"""Замещения должностей (модуль 3 ТЗ).

Администрация создаёт запись: отсутствующий → замещающий, причина, период.
Флаги «передать задачи / планы / ВШК» — переназначение объектов с записью в
историю (audit + human-формулировки) и уведомлениями участникам.
Замещающий подтверждает принятие ПРОСТЫМ КЛИКОМ (SubstitutionStatus.CONFIRMED) —
и ТОЛЬКО тогда объекты передаются (transferred_at). При отмене, завершении и
удалении строки переданное возвращается отсутствующему (см. app/services/substitutions.py).
Журнал замещений — для отчётности, с экспортом в CSV.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import SubstitutionStatus
from app.types import GUID, EnumString


class Substitution(Base):
    __tablename__ = "substitutions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    absent_user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    substitute_user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # флаги передачи
    transfer_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transfer_plans: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transfer_vshk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[SubstitutionStatus] = mapped_column(
        EnumString(SubstitutionStatus, 20), nullable=False, default=SubstitutionStatus.ACTIVE, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Момент фактической передачи объектов замещающему (v1.11: передача выполняется
    # НЕ при создании, а при подтверждении замещающим). Не NULL — объекты сейчас у
    # замещающего; при возврате (отмена / завершение / удаление строки) — снова NULL.
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    initiator_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text)  # итоговая сводка переданного / комментарий отмены

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    absent_user: Mapped["User"] = relationship(foreign_keys=[absent_user_id], lazy="joined")  # noqa: F821
    substitute_user: Mapped["User"] = relationship(foreign_keys=[substitute_user_id], lazy="joined")  # noqa: F821
    initiator: Mapped["User | None"] = relationship(foreign_keys=[initiator_id], lazy="joined")  # noqa: F821
