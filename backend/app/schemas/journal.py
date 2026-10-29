"""Схемы служебных записок, замещений и справки/FAQ (модули 2–4 ТЗ)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import HelpVisibility, NoteStatus, PdfStatus, SubstitutionStatus
from app.schemas.user import UserOut


# ------------------------------------------------------------ служебные записки
class NoteFileOut(BaseModel):
    """Вложение служебной записки.

    v1.12: к каждому документу автоматически готовится PDF-копия, поэтому наружу
    отдаётся её состояние. Интерфейс по нему решает, рисовать ли «Открыть в новой
    вкладке» (READY), «PDF-копия готовится» (PENDING), «не конвертируется»
    (NA — архив) или «доступен только оригинал» (FAILED — нет LibreOffice).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    pdf_status: PdfStatus = PdfStatus.PENDING
    pdf_note: str | None = None
    pdf_ready: bool = False          # свойство модели: копия готова и её можно открыть
    created_at: datetime


class NoteIn(BaseModel):
    subject: str = Field(min_length=3, max_length=300)
    body: str = Field(min_length=10, description="Текст записки")
    executor_id: uuid.UUID | None = None
    approver_id: uuid.UUID | None = Field(
        default=None, description="Кого просят утвердить (Владелец или администрация)")
    deadline: datetime | None = None


class NoteUpdateIn(BaseModel):
    subject: str | None = Field(default=None, min_length=3, max_length=300)
    body: str | None = Field(default=None, min_length=10)
    executor_id: uuid.UUID | None = None
    approver_id: uuid.UUID | None = None
    deadline: datetime | None = None


class NoteApproveIn(BaseModel):
    code: str | None = Field(default=None, min_length=6, max_length=8,
                             description="Одноразовый код из аутентификатора (всегда требуется)")
    word: str | None = Field(default=None, max_length=64,
                             description="Контрольное слово (если задано — политика CODE_WORD)")


class NoteRejectIn(BaseModel):
    comment: str = Field(min_length=3, max_length=1000, description="Комментарий автору (обязателен)")


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int | None
    year: int | None
    subject: str
    status: NoteStatus
    author: UserOut | None = None
    executor: UserOut | None = None
    approver: UserOut | None = None
    deadline: datetime | None = None
    approved_at: datetime | None = None
    approved_sign_method: str | None = None
    reject_comment: str | None = None
    task_id: uuid.UUID | None = None
    created_at: datetime
    files: list[NoteFileOut] = []


class NoteDetailOut(NoteOut):
    body: str


# ------------------------------------------------------------------- замещения
class SubstitutionIn(BaseModel):
    absent_user_id: uuid.UUID
    substitute_user_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)
    start_date: datetime
    end_date: datetime | None = None
    transfer_tasks: bool = False
    transfer_plans: bool = False
    transfer_vshk: bool = False


class SubstitutionCancelIn(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


class SubstitutionExtendIn(BaseModel):
    """Продление периода замещения (больничный продлили)."""
    end_date: datetime
    comment: str | None = Field(default=None, max_length=500)


class SubstitutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    absent_user: UserOut
    substitute_user: UserOut
    initiator: UserOut | None = None
    reason: str
    start_date: datetime
    end_date: datetime | None = None
    transfer_tasks: bool
    transfer_plans: bool
    transfer_vshk: bool
    status: SubstitutionStatus
    confirmed_at: datetime | None = None
    ended_at: datetime | None = None
    # не NULL — объекты фактически переданы замещающему (и ещё не возвращены)
    transferred_at: datetime | None = None
    note: str | None = None
    created_at: datetime


# --------------------------------------------------------------- справка / FAQ
class HelpArticleIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    category: str = Field(default="Общее", max_length=120)
    body: str | None = None
    mandatory_read: bool = False
    # ALL — всем сотрудникам, ADMIN — только администрации, OWNER — только Владельцу.
    # Администрация может задать ALL или ADMIN; OWNER — любое из трёх значений.
    visibility: HelpVisibility = Field(
        default=HelpVisibility.ALL,
        description="Кто видит статью: ALL / ADMIN / OWNER",
    )


class HelpArticleUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    category: str | None = Field(default=None, max_length=120)
    body: str | None = None
    mandatory_read: bool | None = None
    visibility: HelpVisibility | None = None


class HelpAckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user: UserOut
    acked_at: datetime | None = None
    sign_method: str | None = None


class HelpArticleFileOut(BaseModel):
    """Вложение инструкции (v1.12): документов у одной статьи может быть несколько.

    `pdf_status` / `pdf_ready` описывают автоматическую PDF-копию — по ним
    интерфейс решает, рисовать ли «Открыть в новой вкладке» или только «Скачать».
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    caption: str | None = None          # «Приложение 1», «Форма заявления»
    pdf_status: PdfStatus = PdfStatus.PENDING
    pdf_note: str | None = None
    pdf_ready: bool = False
    created_at: datetime
    uploaded_by: UserOut | None = None


class HelpArticleOut(BaseModel):
    """Статья справки/инструкция.

    v1.12: `files` — список вложений (их может быть несколько). Поля
    `file_name` / `file_mime` / `file_size` сохранены как НАСЛЕДИЕ: они
    заполняются по первому вложению, чтобы прежние клиенты и выгрузки не
    сломались. Новому коду следует читать `files`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    slug: str | None = None
    title: str
    category: str
    body: str | None = None
    files: list[HelpArticleFileOut] = []
    # --- наследие до v1.12: первый файл (заполняется в API) ---
    file_name: str | None = None
    file_mime: str | None = None
    file_size: int | None = None
    mandatory_read: bool
    visibility: HelpVisibility = HelpVisibility.ALL
    created_by: UserOut | None = None
    updated_at: datetime
    # для обязательных инструкций: моя отметка и лист ознакомления (админу)
    my_ack_at: datetime | None = None
    acks: list[HelpAckOut] | None = None

    @model_validator(mode="after")
    def _legacy_file_fields(self) -> "HelpArticleOut":
        """Подставляет данные первого вложения в прежние поля.

        Делаем ПОСЛЕ валидации (mode='after'), а не до: мутировать ORM-объект
        внутри before-валидатора — значит писать в него атрибуты, которых нет
        в модели, и получать неожиданные эффекты при повторном использовании
        того же объекта в запросе.
        """
        if not self.files:
            return self
        first = self.files[0]
        if self.file_name is None:
            self.file_name = first.original_name
        if self.file_mime is None:
            self.file_mime = first.mime_type
        if self.file_size is None:
            self.file_size = first.size_bytes
        return self
