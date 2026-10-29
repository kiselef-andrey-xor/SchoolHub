"""Справка и инструкции / FAQ (модуль 4 ТЗ).

Два вида карточек:
  * SYSTEM — встроенные (пароль, роли, подпись, локдаун): seed в app/services/help.py,
    редактируются только Владельцем;
  * UPLOAD — загружаемые инструкции администрации (файл PDF/DOCX во встроенном хранилище).

Категории, полнотекстовый поиск (название+текст+категория), попадание в глобальный поиск.
v1.12: к инструкции прикрепляется НЕСКОЛЬКО документов (`HelpArticleFile`), каждый
автоматически приводится к PDF и открывается кликом в новой вкладке.
Флаг ``mandatory_read`` у инструкции создаёт ЛИСТ ОЗНАКОМЛЕНИЯ по механизму приказов
(OrderAcknowledgement с entity «help»): ознакомление — клик либо ПЭП согласно политике.
Поле ``visibility`` ограничивает круг читателей: ``ALL`` — всем сотрудникам,
``ADMIN`` — только администрации, ``OWNER`` — только Владельцу. Статья недоступной
видимости для остальных не существует: её нет в списке, в категориях, в глобальном
поиске, а прямой запрос по id возвращает 404.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import HelpVisibility, PdfStatus
from app.types import GUID, EnumString


class HelpArticle(Base):
    __tablename__ = "help_articles"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="UPLOAD")  # SYSTEM | UPLOAD
    slug: Mapped[str | None] = mapped_column(String(80), unique=True)               # для системных карточек

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="Общее", index=True)
    body: Mapped[str | None] = mapped_column(Text)          # markdown-текст (для SYSTEM и заметок UPLOAD)

    # --- файл инструкции: НАСЛЕДИЕ до v1.12 ---
    # Один файл на статью хранился прямо здесь. С v1.12 вложения — отдельные строки
    # `help_article_files` (их может быть несколько), а эти колонки оставлены как
    # есть для старых выгрузок и НЕ записываются новым кодом. Миграция m027
    # переносит содержимое в `help_article_files`; API отдаёт `files` и
    # совместимое `file_name` первого вложения.
    file_path: Mapped[str | None] = mapped_column(String(1024))
    file_name: Mapped[str | None] = mapped_column(String(512))
    file_mime: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(Integer)

    mandatory_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    # Кому видна статья: ALL (всем) / ADMIN (только администрации) / OWNER (только Владельцу).
    # Значение ограничивает и список рассылки «обязательно к прочтению»: инструкция
    # для администрации не создаёт отметок у учителей (app/services/help.py).
    visibility: Mapped[HelpVisibility] = mapped_column(
        EnumString(HelpVisibility, 10), nullable=False,
        default=HelpVisibility.ALL, server_default=HelpVisibility.ALL.value, index=True,
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id], lazy="joined")   # noqa: F821
    updated_by: Mapped["User | None"] = relationship(foreign_keys=[updated_by_id], lazy="joined")   # noqa: F821
    files: Mapped[list["HelpArticleFile"]] = relationship(
        back_populates="article", cascade="all, delete-orphan", lazy="selectin",
        order_by="HelpArticleFile.created_at",
    )


class HelpArticleFile(Base):
    """Вложение инструкции (v1.12): документов может быть НЕСКОЛЬКО.

    До 1.12 у статьи был один файл прямо в колонках `help_articles.file_*`,
    поэтому к инструкции нельзя было приложить, например, и положение, и форму
    заявления к нему. Теперь файлы — отдельные строки; колонки `file_*` в
    `help_articles` сохранены как наследие и больше не пишутся (миграция m027
    переносит из них данные сюда).

    `pdf_path` — автоматическая PDF-копия: инструкция открывается кликом в новой
    вкладке даже если загружена в DOCX, а на машине администратора нет Word.
    """

    __tablename__ = "help_article_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("help_articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))

    # подпись к файлу: «Приложение 1», «Форма заявления» — видно в списке вложений
    caption: Mapped[str | None] = mapped_column(String(300))

    # --- автоматическая PDF-копия ---
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

    article: Mapped["HelpArticle"] = relationship(back_populates="files")
    uploaded_by: Mapped["User | None"] = relationship(lazy="joined")   # noqa: F821

    @property
    def pdf_ready(self) -> bool:
        """PDF-копия готова и её можно открыть в новой вкладке (v1.12)."""
        return self.pdf_status == PdfStatus.READY and bool(self.pdf_path)


class HelpAck(Base):
    """Отметка об ознакомлении с обязательной инструкцией (лист ознакомления)."""

    __tablename__ = "help_acks"
    __table_args__ = (UniqueConstraint("article_id", "user_id", name="uq_help_ack_article_user"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("help_articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sign_method: Mapped[str | None] = mapped_column(String(20))   # AckSignMethod: CLICK / TOTP / CODE_WORD
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    article: Mapped["HelpArticle"] = relationship(lazy="joined")
    user: Mapped["User"] = relationship(lazy="joined")            # noqa: F821
