"""Схемы Helpdesk: заявки сотрудников в техническую поддержку школы (v1.12).

Принцип тот же, что у остального API: In-схемы валидируют формат, а бизнес-ограничения
(кто может взять в работу, какие переходы статусов разрешены) проверяет сервисный слой —
тогда ошибка приходит понятным кодом (409 TICKET_TRANSITION), а не «422 value is not a
valid enum».
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import PdfStatus, TicketCategory, TicketPriority, TicketStatus
from app.schemas.user import UserOut


# --------------------------------------------------------------------- вложения
class TicketFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    kind: str = "AUTHOR"
    # состояние автоматической PDF-копии: UI показывает «готовится» / «не конвертируется»
    pdf_status: PdfStatus = PdfStatus.PENDING
    pdf_note: str | None = None
    # `pdf_ready` — свойство модели: по нему фронт решает, рисовать ли
    # «Открыть в новой вкладке» или только «Скачать»
    pdf_ready: bool = False
    created_at: datetime
    uploaded_by: UserOut | None = None


# ------------------------------------------------------------------- переписка
class TicketMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str = "MESSAGE"
    body: str
    author: UserOut | None = None
    created_at: datetime


class TicketMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000, description="Текст реплики")


# --------------------------------------------------------------------- заявки
class TicketIn(BaseModel):
    """Создание заявки. Автор — любой сотрудник, поэтому форма намеренно короткая:
    чем больше полей, тем меньше заявок дойдёт до администратора."""

    title: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=5, description="Что случилось, когда началось, что уже пробовали")
    category: TicketCategory = TicketCategory.OTHER
    priority: TicketPriority = TicketPriority.NORMAL
    location: str | None = Field(default=None, max_length=200,
                                 description="Где находится оборудование: «кабинет 214», «актовый зал»")
    contact_phone: str | None = Field(default=None, max_length=40,
                                      description="Телефон для связи — администратору часто быстрее позвонить")
    equipment_id: uuid.UUID | None = Field(default=None, description="Единица из модуля «Инвентарь»")


class TicketUpdateIn(BaseModel):
    """Правка заявки: автор — пока заявка не закрыта; администратор — всегда
    (уточнить категорию/приоритет ему приходится регулярно)."""

    title: str | None = Field(default=None, min_length=3, max_length=300)
    description: str | None = Field(default=None, min_length=5)
    category: TicketCategory | None = None
    priority: TicketPriority | None = None
    location: str | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=40)
    equipment_id: uuid.UUID | None = None


class TicketTakeIn(BaseModel):
    """Взять в работу / переназначить другому системному администратору."""

    assignee_id: uuid.UUID | None = Field(
        default=None,
        description="Кому назначить; пусто — взять себе (только для системного администратора)",
    )
    comment: str | None = Field(default=None, max_length=4000)


class TicketResolveIn(BaseModel):
    """Заявка выполнена: что сделано (обязательно — автор по этому тексту принимает работу)."""

    resolution: str = Field(min_length=3, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)


class TicketRejectIn(BaseModel):
    """Отклонение: обязательно с причиной (не наш профиль, дубликат, нужна служебка)."""

    reason: str = Field(min_length=3, max_length=1000)


class TicketCloseIn(BaseModel):
    """Подтверждение автора («всё работает») или возврат на доработку."""

    comment: str | None = Field(default=None, max_length=4000)


class TicketReopenIn(BaseModel):
    """Автор вернул выполненную заявку: что не так."""

    comment: str = Field(min_length=3, max_length=4000)


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int | None = None
    year: int | None = None
    title: str
    description: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    location: str | None = None
    contact_phone: str | None = None
    equipment_id: uuid.UUID | None = None
    equipment_name: str | None = None

    author: UserOut | None = None
    assignee: UserOut | None = None
    closed_by: UserOut | None = None
    auto_closed: bool = False

    taken_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    resolution: str | None = None
    rejected_reason: str | None = None

    sla_hours: int | None = None
    # вычисляемые на сервере показатели (в БД не хранятся — устаревают сами)
    sla_deadline: datetime | None = None
    sla_breached: bool = False
    reaction_minutes: int | None = None
    work_minutes: int | None = None

    created_at: datetime
    updated_at: datetime
    files: list[TicketFileOut] = []
    messages: list[TicketMessageOut] = []
    # заполняется в API: момент последней реплики (для сортировки «кто давно молчит»)
    last_message_at: datetime | None = None


class TicketListOut(TicketOut):
    """Элемент журнала: тот же контракт, список заявок отдаётся полными карточками.

    Отдельный класс оставлен как точка расширения (например, убрать `messages`
    из списка, если журнал станет большим).
    """


class TicketStatsOut(BaseModel):
    """Сводка для шапки раздела и дашборда."""

    total: int = 0
    new: int = 0
    in_progress: int = 0
    resolved: int = 0
    closed: int = 0
    rejected: int = 0
    cancelled: int = 0
    mine: int = 0                 # в работе у меня
    sla_breached: int = 0         # открытые с просроченной реакцией
    resolved_today: int = 0
    median_reaction_minutes: int | None = None
    is_staff: bool = False        # видит ли текущий пользователь всю очередь


class TicketDirectoryOut(BaseModel):
    """Справочники для формы заявки (категории, приоритеты, статусы)."""

    categories: list[dict[str, str]]
    priorities: list[dict[str, str]]
    statuses: list[dict[str, str]]
    sla_hours: dict[str, int]
