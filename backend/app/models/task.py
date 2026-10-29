"""Task Manager: задача, исполнители (несколько), вложения, комментарии, история статусов.

Модель v1.2: у задачи НЕСКОЛЬКО исполнителей (task_assignees), у каждого — своя
отметка «выполнено» (done_at). Когда отметки поставили все — задача автоматически
закрывается (DONE); снятие отметки возвращает задачу в IN_PROGRESS.

Модель v1.3: повторяющиеся задачи (recurrence). Одна строка в БД; после
выполнения в текущем периоде (день / неделя / месяц) задача автоматически
возвращается в NEW — см. app/services/tasks.py::rollover_due.

Модель v1.10: история повторов (task_occurrences). Каждый ЗАКРЫТЫЙ период
повторяющейся задачи фиксируется строкой DONE (выполнен) или MISSED (провален —
период истёк, а задачу так и не выполнили). Проваленные периоды закрывает
rollover_due; выполнение — record_occurrence_done. Подробности — ADR-15
в docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import uuid
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base
from app.enums import PdfStatus, TaskStatus
from app.models.user import User
from app.types import GUID, EnumString


class TaskPriority:
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"

    ALL = (LOW, NORMAL, HIGH, URGENT)


class TaskRecurrence:
    """Повторяемость задачи.

    NONE    — обычная задача (как раньше);
    DAILY   — каждый день;
    WEEKLY  — по дням недели (recurrence_weekdays: 1=пн … 7=вс);
    MONTHLY — ежемесячно, в число recurrence_day (1..31; 29-31 в короткие
              месяцы переносятся на последний день месяца).

    В БД — ОДНА строка задачи (история не плодится): после выполнения в
    текущем периоде задача автоматически возвращается в NEW к началу
    следующего периода (см. app/services/tasks.py::rollover_due).
    """

    NONE = "NONE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"

    ALL = (NONE, DAILY, WEEKLY, MONTHLY)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    incoming_number: Mapped[str | None] = mapped_column(String(120))  # входящий номер письма
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[TaskStatus] = mapped_column(
        EnumString(TaskStatus, 20), nullable=False, default=TaskStatus.NEW, index=True
    )
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskPriority.NORMAL, index=True)

    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Может быть пустым для «общих» задач без дедлайна; OVERDUE вычисляется только при его наличии.
    # У повторяющихся задач дедлайна нет: срок задаёт сам период повторения.
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # --- повторяемость (v1.3) ---
    recurrence: Mapped[str] = mapped_column(
        String(10), nullable=False, default=TaskRecurrence.NONE, server_default=TaskRecurrence.NONE, index=True
    )
    # для WEEKLY: список дней недели ISO (1=пн … 7=вс)
    recurrence_weekdays: Mapped[list[Any] | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"))
    # для MONTHLY: число месяца (1..31, переносится на последний день коротких месяцев)
    recurrence_day: Mapped[int | None] = mapped_column(Integer)
    # когда задача последний раз была выполнена ПОЛНОСТЬЮ (переживает автосброс периода)
    last_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- история повторов (v1.10) ---
    # Курсор: начало первого НЕЗАКРЫТОГО периода (локальная дата). NULL — история ещё
    # не начата (старые задачи): инициализируется при первом же rollover, задним числом
    # периоды не проваливаются.
    occurrence_cursor: Mapped[date_cls | None] = mapped_column(Date)
    # Сколько периодов подряд провалено (0 — последний закрытый период выполнен).
    missed_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Последний закрытый период — денормализация, чтобы бейдж «провалено» в списке
    # задач не делал отдельный запрос к task_occurrences.
    last_occurrence_start: Mapped[date_cls | None] = mapped_column(Date)
    last_occurrence_status: Mapped[str | None] = mapped_column(String(8))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- relations ---
    creator: Mapped["User | None"] = relationship(back_populates="created_tasks", foreign_keys=[creator_id], lazy="joined")
    assignees: Mapped[list["TaskAssignee"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TaskAssignee.created_at",
    )
    files: Mapped[list["TaskFile"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TaskFile.created_at",
    )
    comments: Mapped[list["TaskComment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskComment.created_at"
    )
    history: Mapped[list["TaskStatusHistory"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskStatusHistory.created_at"
    )
    occurrences: Mapped[list["TaskOccurrence"]] = relationship(
        back_populates="task", cascade="all, delete-orphan",
        order_by=lambda: TaskOccurrence.period_start.desc(),
    )

    # --- derived ---
    @property
    def assignee_users(self) -> list["User"]:
        return [a.user for a in self.assignees if a.user is not None]

    @property
    def assignee_ids(self) -> list[uuid.UUID]:
        return [a.user_id for a in self.assignees]

    @property
    def marks_done(self) -> int:
        return sum(1 for a in self.assignees if a.done_at is not None)

    def mark_of(self, user_id: uuid.UUID) -> "TaskAssignee | None":
        for a in self.assignees:
            if a.user_id == user_id:
                return a
        return None


class TaskAssignee(Base):
    """Исполнитель задачи + его личная отметка «выполнено» (done_at).

    Одна строка на (задача × сотрудник). done_at=None — отметки нет.
    """

    __tablename__ = "task_assignees"
    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_task_assignee"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="assignees")
    user: Mapped["User"] = relationship(lazy="joined")


class TaskFile(Base):
    """Вложение задачи (методичка, ведомость, фото…). Файл неизменяем: замена = новая строка.

    v1.12: `pdf_path` — автоматическая PDF-копия документа. Заполняется у файлов,
    перенесённых из служебной записки (там копия готовится при загрузке), и лениво —
    при первом инлайн-просмотре (см. app/services/document_pdf.py). Оригинал не
    удаляется: PDF нужен для просмотра в новой вкладке и для печати, исходник —
    чтобы отдать документ в авторском формате.
    """

    __tablename__ = "task_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))          # sha256

    # --- автоматическая PDF-копия (v1.12) ---
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    pdf_size: Mapped[int | None] = mapped_column(Integer)
    pdf_status: Mapped[PdfStatus] = mapped_column(
        EnumString(PdfStatus, 10), nullable=False,
        default=PdfStatus.PENDING, server_default=PdfStatus.PENDING.value,
    )
    pdf_note: Mapped[str | None] = mapped_column(String(300))

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="files")
    uploaded_by: Mapped["User | None"] = relationship(lazy="joined")

    @property
    def pdf_ready(self) -> bool:
        """PDF-копия готова и её можно открыть в новой вкладке (v1.12)."""
        return self.pdf_status == PdfStatus.READY and bool(self.pdf_path)


class TaskComment(Base):

    __tablename__ = "task_comments"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="comments")
    author: Mapped["User | None"] = relationship(lazy="joined")


class TaskStatusHistory(Base):
    """Аудит-след по жизненному циклу задачи (кто и когда перевёл статус)."""

    __tablename__ = "task_status_history"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="history")
    actor: Mapped["User | None"] = relationship(lazy="joined")


class TaskOccurrence(Base):
    """Закрытый период повторяющейся задачи (v1.10).

    Строка создаётся, когда период ПОЛНОСТЬЮ закрыт:
      * DONE   — задачу выполнили до конца периода (record_occurrence_done);
      * MISSED — период истёк, задача не выполнена (закрытие в rollover_due).

    Текущий (незакрытый) период в таблице НЕ хранится — он живёт в самой задаче
    (effective_status / done_this_period). UNIQUE(task_id, period_start) защищает
    от двойного закрытия.
    """

    __tablename__ = "task_occurrences"
    __table_args__ = (UniqueConstraint("task_id", "period_start", name="uq_occurrences_task_period"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # локальная дата начала периода, который закрывает строка
    period_start: Mapped[date_cls] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)  # DONE | MISSED
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # кто закрыл период (id пользователей с отметкой done_at / поставивший DONE)
    completed_by_ids: Mapped[list[Any] | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="occurrences")
