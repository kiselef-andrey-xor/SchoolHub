"""Helpdesk — заявки сотрудников в техническую поддержку школы (v1.12).

Зачем отдельный модуль, а не «задача» или «служебная записка»:
  * **автор — любой сотрудник** (учитель), а задачу создавать могут только
    администрация и Владелец; служебка же требует согласования и подписи кодом,
    что для «зажевало принтер» избыточно;
  * **исполнитель — не конкретный человек, а очередь**: заявку видит каждый
    системный администратор (грант Владельца `users.helpdesk_access`) и сам
    берёт её в работу. Назначение сверху здесь мешало бы;
  * **жизненный цикл короче и с подтверждением автора**: NEW → IN_PROGRESS →
    RESOLVED → CLOSED, причём автор может вернуть заявку на доработку.

Жизненный цикл (state machine `app.enums.TICKET_TRANSITIONS`):

    NEW ─────────────► IN_PROGRESS ─────────► RESOLVED ─────────► CLOSED
     │  (взял в работу)     ▲   (выполнено)       │  (автор подтвердил)
     │                      └─────────────────────┘  (вернул на доработку)
     ├─► REJECTED  (администратор отклонил с комментарием)
     └─► CANCELLED (автор отозвал)

Норматив РЕАКЦИИ (`app.enums.TICKET_SLA_HOURS`) считается от создания до взятия
в работу по приоритету: URGENT 2 ч, HIGH 4 ч, NORMAL 8 ч, LOW 24 ч. Просрочка
реакции — вычисляемое состояние (в БД не хранится, см. services/helpdesk.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import PdfStatus, TicketCategory, TicketMessageKind, TicketPriority, TicketStatus
from app.types import GUID, EnumString


class HelpdeskTicket(Base):
    """Заявка в техподдержку."""

    __tablename__ = "helpdesk_tickets"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    # № п/п за год — как у служебных записок: журнал должен быть цитируемым
    # («заявка №14/2026»), а не только «та с принтером от 12 марта»
    number: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer, index=True)

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[TicketCategory] = mapped_column(
        EnumString(TicketCategory, 20), nullable=False,
        default=TicketCategory.OTHER, server_default=TicketCategory.OTHER.value, index=True,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        EnumString(TicketPriority, 10), nullable=False,
        default=TicketPriority.NORMAL, server_default=TicketPriority.NORMAL.value, index=True,
    )
    status: Mapped[TicketStatus] = mapped_column(
        EnumString(TicketStatus, 20), nullable=False,
        default=TicketStatus.NEW, server_default=TicketStatus.NEW.value, index=True,
    )

    # где находится проблемное оборудование: «кабинет 214», «актовый зал», «приёмная».
    # Автор знает это лучше любого справочника, поэтому — свободный текст.
    location: Mapped[str | None] = mapped_column(String(200))
    # обратный телефон: администратору часто быстрее позвонить, чем переписываться
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    # инвентарный номер оборудования (модуль «Инвентарь»): необязательная, но
    # полезная связка — по номеру видно историю обслуживания
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("equipment_items.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- исполнитель (системный администратор, взявший заявку) ---
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # кто закрыл: автор подтвердил или сработало автозакрытие (housekeeping)
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    auto_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # чем закончилось: текст решения (обязателен для RESOLVED) или причина отклонения
    resolution: Mapped[str | None] = mapped_column(Text)
    rejected_reason: Mapped[str | None] = mapped_column(String(1000))

    # норматив реакции в часах, зафиксированный на момент создания. Копируется
    # из TICKET_SLA_HOURS, чтобы правка норматива в коде не переписывала историю:
    # заявка 2026 года оценивается по нормативам 2026 года.
    sla_hours: Mapped[int | None] = mapped_column(Integer)
    # когда сисадминам ушло напоминание о просрочке реакции (одно на заявку:
    # housekeeping работает часто, а письма каждые полчаса свели бы с ума)
    sla_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # --- relations ---
    author: Mapped["User | None"] = relationship(foreign_keys=[author_id], lazy="joined")      # noqa: F821
    assignee: Mapped["User | None"] = relationship(foreign_keys=[assignee_id], lazy="joined")  # noqa: F821
    closed_by: Mapped["User | None"] = relationship(foreign_keys=[closed_by_id], lazy="joined")  # noqa: F821
    equipment: Mapped["EquipmentItem | None"] = relationship(lazy="joined")                     # noqa: F821
    messages: Mapped[list["HelpdeskMessage"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", lazy="selectin",
        order_by="HelpdeskMessage.created_at",
    )
    files: Mapped[list["HelpdeskFile"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", lazy="selectin",
        order_by="HelpdeskFile.created_at",
    )

    # --- derived ---
    @property
    def label(self) -> str:
        return f"{self.number}/{self.year}" if self.number else "без номера"

    @property
    def equipment_name(self) -> str | None:
        """Название единицы инвентаря плоским полем: в списке заявок вложенный
        объект не нужен, а связь уже загружена (lazy='joined')."""
        return self.equipment.name if self.equipment is not None else None


class HelpdeskMessage(Base):
    """Переписка по заявке + служебные отметки (смена статуса, назначение).

    Одна таблица на оба случая намеренно: в интерфейсе это одна лента событий,
    а различает их `kind`. Реплики автора и администратора остаются репликами,
    системные строки пишутся сервисом и не редактируются.
    """

    __tablename__ = "helpdesk_messages"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("helpdesk_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[TicketMessageKind] = mapped_column(
        EnumString(TicketMessageKind, 10), nullable=False,
        default=TicketMessageKind.MESSAGE, server_default=TicketMessageKind.MESSAGE.value,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["HelpdeskTicket"] = relationship(back_populates="messages")
    author: Mapped["User | None"] = relationship(lazy="joined")  # noqa: F821


class HelpdeskFile(Base):
    """Вложение заявки: фото поломки от автора, акт/счёт от администратора.

    `pdf_path` — автоматическая PDF-копия (v1.12): любой загруженный документ
    приводится к PDF, чтобы его можно было открыть кликом в новой вкладке
    и приложить к заявке при печати/экспорте. Оригинал при этом НЕ удаляется.
    """

    __tablename__ = "helpdesk_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("helpdesk_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="AUTHOR")  # TicketFileKind

    # --- автоматическая PDF-копия ---
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    pdf_size: Mapped[int | None] = mapped_column(Integer)
    pdf_status: Mapped[PdfStatus] = mapped_column(
        EnumString(PdfStatus, 10), nullable=False,
        default=PdfStatus.PENDING, server_default=PdfStatus.PENDING.value,
    )
    pdf_note: Mapped[str | None] = mapped_column(String(300))   # почему не сконвертировалось

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["HelpdeskTicket"] = relationship(back_populates="files")
    uploaded_by: Mapped["User | None"] = relationship(lazy="joined")  # noqa: F821

    @property
    def pdf_ready(self) -> bool:
        """PDF-копия готова и её можно открыть в новой вкладке."""
        return self.pdf_status == PdfStatus.READY and bool(self.pdf_path)
