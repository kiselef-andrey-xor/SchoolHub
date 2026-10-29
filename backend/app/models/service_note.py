"""Служебные записки (модуль 2 ТЗ).

Записка пишется как задача: автор, тема, текст, вложения, исполнитель, срок.
Цикл (state machine NOTE_TRANSITIONS):
    DRAFT → ON_APPROVAL → APPROVED (подпись кодом TOTP, всегда) → COMPLETED
                        ↘ REJECTED (возврат автору с комментарием; после доработки — снова ON_APPROVAL)

При утверждении из записки АВТОМАТИЧЕСКИ создаётся задача исполнителю — ВМЕСТЕ
с вложениями: файлы физически копируются в хранилище, поэтому удаление записки
не оставляет исполнителя без документа (v1.12). Ссылки взаимные: note.task_id ↔
tasks.description содержит ссылку на записку, а в истории задачи — пометка
о происхождении (origin_note_id в payload уведомления).

v1.12: каждый загруженный документ автоматически приводится к PDF (см.
app/services/document_pdf.py) — копия лежит рядом с оригиналом в `pdf_path`,
по клику документ открывается в новой вкладке браузера.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import NoteStatus, PdfStatus
from app.types import GUID, EnumString


class ServiceNote(Base):
    __tablename__ = "service_notes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    number: Mapped[int | None] = mapped_column(Integer)          # порядковый номер за год (журнал)
    year: Mapped[int | None] = mapped_column(Integer, index=True)

    author_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[NoteStatus] = mapped_column(
        EnumString(NoteStatus, 20), nullable=False, default=NoteStatus.DRAFT, index=True
    )

    # согласующий (по умолчанию — Владелец); подпись при утверждении — всегда код TOTP
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_sign_method: Mapped[str | None] = mapped_column(String(20))  # AckSignMethod.TOTP
    reject_comment: Mapped[str | None] = mapped_column(String(1000))

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # --- relations ---
    author: Mapped["User | None"] = relationship(foreign_keys=[author_id], lazy="joined")  # noqa: F821
    executor: Mapped["User | None"] = relationship(foreign_keys=[executor_id], lazy="joined")  # noqa: F821
    approver: Mapped["User | None"] = relationship(foreign_keys=[approver_id], lazy="joined")  # noqa: F821
    files: Mapped[list["ServiceNoteFile"]] = relationship(
        back_populates="note", cascade="all, delete-orphan", lazy="selectin",
        order_by="ServiceNoteFile.created_at",
    )


class ServiceNoteFile(Base):
    """Вложение служебной записки.

    Файл неизменяем: замена — новая строка (как у вложений задач).

    v1.12: к КАЖДОМУ загруженному документу автоматически готовится PDF-копия
    (`pdf_path`). Оригинал не удаляется — он нужен, чтобы отдать авторский DOCX
    или фотографию в исходном качестве. Копия делается для двух вещей:
      * документ открывается кликом в новой вкладке (браузеры не рендерят DOCX);
      * при утверждении записка передаётся исполнителю вместе с файлом — а задача
        с PDF-вложением печатается и просматривается без LibreOffice на клиенте.
    """

    __tablename__ = "service_note_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("service_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))

    # --- автоматическая PDF-копия (v1.12) ---
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

    note: Mapped["ServiceNote"] = relationship(back_populates="files")

    @property
    def pdf_ready(self) -> bool:
        """PDF-копия готова и её можно открыть в новой вкладке (v1.12)."""
        return self.pdf_status == PdfStatus.READY and bool(self.pdf_path)
