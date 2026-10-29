"""Схемы Task Manager (v1.2: несколько исполнителей, отметки «выполнено», вложения)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import PdfStatus, TaskStatus
from app.schemas.user import UserOut


class TaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    description: str | None = None
    incoming_number: str | None = Field(default=None, max_length=120,
                                        description="Входящий номер письма — попадёт в журнал входящих")
    reg_no: int | None = Field(default=None, ge=1, le=9999999,
                               description="Номер регистрации в журнале входящих (необязательно; "
                                           "пусто — присвоится автоматически по порядку за год)")
    assignee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    deadline: datetime | None = None
    priority: str = "NORMAL"
    recurrence: str = Field(default="NONE", description="NONE | DAILY | WEEKLY | MONTHLY")
    recurrence_weekdays: list[int] | None = Field(
        default=None, description="Для WEEKLY: дни недели ISO, 1=пн … 7=вс"
    )
    recurrence_day: int | None = Field(default=None, ge=1, le=31, description="Для MONTHLY: число месяца")


class TaskCreateIn(TaskIn):
    pass


class TaskUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    description: str | None = None
    assignee_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    deadline: datetime | None = None
    priority: str | None = None
    status: TaskStatus | None = None
    incoming_number: str | None = Field(default=None, max_length=120)
    reg_no: int | None = Field(default=None, ge=1, le=9999999)
    recurrence: str | None = Field(default=None, description="NONE | DAILY | WEEKLY | MONTHLY")
    recurrence_weekdays: list[int] | None = None
    recurrence_day: int | None = Field(default=None, ge=1, le=31)


class StatusChangeIn(BaseModel):
    status: TaskStatus
    note: str | None = Field(default=None, max_length=500)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    body: str
    created_at: datetime
    author: UserOut | None = None


class StatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status: str | None
    to_status: str
    note: str | None
    created_at: datetime
    actor: UserOut | None = None


class TaskAssigneeOut(BaseModel):
    """Исполнитель задачи + его личная отметка «выполнено»."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user: UserOut
    done_at: datetime | None = None


class TaskFileOut(BaseModel):
    """Вложение задачи.

    v1.12: `pdf_status` / `pdf_ready` — состояние автоматической PDF-копии. Она
    приходит вместе с документом из служебной записки (см.
    `service_notes.copy_files_to_task`) и позволяет открыть вложение в новой
    вкладке прямо из карточки задачи.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    checksum: str | None = None
    pdf_status: PdfStatus = PdfStatus.PENDING
    pdf_note: str | None = None
    pdf_ready: bool = False
    created_at: datetime
    uploaded_by: UserOut | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    incoming_number: str | None = None
    incoming_reg: str | None = None         # «12/2026» — номер регистрации из журнала входящих
    status: TaskStatus                      # хранимый статус
    effective_status: str                   # NEW | IN_PROGRESS | DONE | OVERDUE (вычисляется)
    priority: str
    deadline: datetime | None
    recurrence: str = "NONE"                # NONE | DAILY | WEEKLY | MONTHLY
    recurrence_weekdays: list[int] | None = None   # 1=пн … 7=вс (для WEEKLY)
    recurrence_day: int | None = None              # число месяца (для MONTHLY)
    last_done_at: datetime | None = None           # последнее полное выполнение (переживает автосброс)
    done_this_period: bool = False                 # повторяющаяся задача выполнена в текущем периоде
    missed_streak: int = 0                         # сколько периодов подряд провалено (v1.10)
    last_occurrence_start: date | None = None      # начало последнего закрытого периода
    last_occurrence_status: str | None = None      # DONE | MISSED
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    creator: UserOut | None = None
    assignees: list[TaskAssigneeOut] = []
    is_overdue: bool = False
    days_left: int | None = None
    can_edit: bool = False
    can_mark: bool = False                  # я — исполнитель и могу поставить/снять отметку
    my_done: bool = False                   # моя отметка «выполнено» уже стоит
    marks_done: int = 0                     # сколько исполнителей отметилось
    files_count: int = 0


class TaskDetailOut(TaskOut):
    comments: list[CommentOut] = []
    history: list[StatusHistoryOut] = []
    files: list[TaskFileOut] = []


class TaskOccurrenceOut(BaseModel):
    """Закрытый период повторяющейся задачи (v1.10): выполнен или провален."""

    period_start: date                           # локальная дата начала периода
    status: str                                  # DONE | MISSED
    completed_at: datetime | None = None         # когда выполнили (для DONE)
    completed_by: list[str] = []                 # имена закрывших период (для DONE)
    closed_at: datetime                          # когда период закрыт
