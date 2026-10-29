"""Пользователь + роли.

Роль хранится как строковый enum (а не FK на таблицу roles) осознанно:
ролей три, они зафиксированы бизнес-требованием, а проверки прав должны быть
дешёвыми и типизированными. Расширение до динамических прав — через таблицу
permissions + many-to-many (миграция описана в README).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base
from app.enums import Role
from app.types import GUID, EnumString

# Маркер «пароль не применим»: для доменных учётках проверка пароля всегда неуспешна.
UNUSABLE_PASSWORD = "!ldap"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # e-mail НЕ обязателен: школа может завести учётку «ФИО + пароль». Тогда вход —
    # по автоматически сгенерированному логину (транслитерация ФИО, см. services/logins.py).
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    login: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)  # логин для входа без e-mail
    avatar: Mapped[str | None] = mapped_column(String(512))  # путь в файловом хранилище (upload_dir)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    birth_date: Mapped[date | None] = mapped_column(Date)   # дата рождения (необязательно): виджет «Дни рождения»
    role: Mapped[Role] = mapped_column(EnumString(Role, 20), nullable=False, default=Role.TEACHER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # «удалён»: учётка скрыта из всех списков и не может войти, но ИСТОРИЯ
    # (приказы, ознакомления, задачи) сохраняется — документ школы не теряется.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # владелец потребовал сменить пароль:Until flag снят, интерфейс принуждает к смене
    password_must_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца на доступ к журналу входящих писем (по умолчанию журнал только у Владельца)
    incoming_journal_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца на раздел «Движение обучающихся»
    movement_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца на раздел «Учёт оборудования»
    equipment_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца на журнал служебных записок ПОЛНОСТЬЮ (по умолчанию — только свои)
    notes_full_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца «кадровик»: журнал замещений целиком + удаление строк замещений.
    # Должность значения не имеет (классный руководитель, руководитель ШМО и т.п.) —
    # важен сам факт назначения Владельцем (v1.11).
    substitutions_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # грант Владельца «системный администратор» (v1.12): очередь заявок Helpdesk —
    # видит ВСЕ заявки, берёт в работу, выполняет, отклоняет. Должность и роль не
    # важны: сисадмином может быть и учитель информатики с ролью TEACHER.
    helpdesk_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- интеграция с доменом (Active Directory / LDAP) ---
    # auth_source='local'  -> пароль в password_hash (bcrypt), вход только локальный
    # auth_source='ldap'   -> пароль проверяет контроллер домена, password_hash = UNUSABLE_PASSWORD
    auth_source: Mapped[str] = mapped_column(String(16), nullable=False, default="local", index=True)
    domain_account: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)  # sAMAccountName
    dn: Mapped[str | None] = mapped_column(String(512))                                       # distinguishedName
    ad_groups: Mapped[list[Any] | None] = mapped_column(JSONB().with_variant(JSON, "sqlite")) # имена групп AD
    last_ad_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- ПЭП через аутентификатор (TOTP, RFC 6238) ---
    # Секрет выдаётся ОДНОРАЗОВО на этапе привязки и больше никогда не возвращается API;
    # пока totp_confirmed_at пуст — привязка не завершена (подтверждение первым кодом).
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512))     # Fernet(secret + "|" + контрольное слово)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # восстановление из копии / очистка аудита / локдаун / удаление пользователя — «код + слово»
    recovery_word_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- relations ---
    created_tasks: Mapped[list["Task"]] = relationship(  # noqa: F821
        back_populates="creator", foreign_keys="Task.creator_id"
    )
    task_assignments: Mapped[list["TaskAssignee"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    acknowledgements: Mapped[list["OrderAcknowledgement"]] = relationship(  # noqa: F821
        back_populates="user"
    )
    # должности-роли из справочника positions (может быть несколько одновременно)
    positions: Mapped[list["Position"]] = relationship(  # noqa: F821
        secondary="user_positions",
        lazy="selectin",
        order_by="Position.sort_order",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} {self.role}>"
