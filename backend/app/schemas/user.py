"""Схемы аутентификации и пользователей."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.enums import Role
from app.schemas.position import PositionOut

# --------------------------------------------------------------------- auth
LOGIN_RE = re.compile(r"^[A-Za-z0-9._@\\-]{2,254}$")


class LoginIn(BaseModel):
    """Логин: e-mail SchoolHub **или** доменная учётка (`ivanov`, `ivanov@school.local`, `SCHOOL\\ivanov`).
    Валидатор намеренно строгий: значение уходит в LDAP-фильтр, поэтому допускаются
    только безопасные символы (дополнительно экранируется в `escape_filter_chars`).
    """

    email: str = Field(min_length=2, max_length=254, examples=["owner@schoolhub.ru", "ivanov@school.local"])
    password: str = Field(min_length=1, max_length=128)
    remember: bool = Field(default=True, description="Держать сессию 14 дней; иначе — до закрытия браузера")

    @field_validator("email")
    @classmethod
    def _validate_login(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not LOGIN_RE.match(cleaned):
            raise ValueError("Логин содержит недопустимые символы")
        return cleaned


class TokenPair(BaseModel):
    """Refresh-токен в теле НЕ отдаётся — он приходит в httpOnly-cookie
    (Set-Cookie на /auth/login и /auth/refresh), недоступной документу и XSS."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 1800


class MeOut(BaseModel):
    """Профиль + набор возможностей (capabilities) для управления UI."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None = None            # не EmailStr: адрес может прийти из AD (любой домен школы)
    login: str | None = None            # логин локальной учётки (если e-mail не задан)
    avatar: str | None = None           # путь в хранилище; URL отдаёт GET /users/{id}/avatar
    full_name: str
    positions: list[PositionOut] = []   # должности-роли из справочника (может быть несколько)
    phone: str | None = None
    birth_date: date | None = None
    role: Role
    is_active: bool
    is_deleted: bool = False
    password_must_change: bool = False
    incoming_journal_access: bool = False
    movement_access: bool = False
    equipment_access: bool = False
    notes_full_access: bool = False
    substitutions_access: bool = False
    helpdesk_access: bool = False       # грант «системный администратор» (Helpdesk, v1.12)
    auth_source: str = "local"          # local | ldap
    domain_account: str | None = None
    ad_groups: list[Any] | None = None
    last_ad_sync_at: datetime | None = None
    capabilities: list[str] = []
    # --- ПЭП (TOTP): состояние привязки аутентификатора; секрет никогда не отдаётся API ---
    totp_enabled: bool = False
    totp_confirmed_at: datetime | None = None
    recovery_word_set: bool = False

    @model_validator(mode="before")
    @classmethod
    def _pep_flags(cls, data):
        """MeOut собирается из ORM-объекта User: поля модели — totp_secret_enc/
        recovery_word_set_at, а в API отдаём вычисляемые флаги (без деталей секрета)."""
        if hasattr(data, "totp_secret_enc"):
            data.totp_enabled = bool(data.totp_enabled and data.totp_confirmed_at)
            data.recovery_word_set = getattr(data, "recovery_word_set_at", None) is not None
        return data
    # --- «Локдаун»: персональные данные подменяются метками для всех, кроме Владельца ---
    @field_serializer("full_name")
    def _mask_full_name(self, v: str) -> str:
        from app.services import lockdown

        return lockdown.mask_employee(str(self.id)) if lockdown.masking() else v

    @field_serializer("phone", "email", "login", "birth_date", "avatar", "domain_account")
    def _mask_private(self, v):
        from app.services import lockdown

        return None if lockdown.masking() else v



# -------------------------------------------------------------------- users
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None = None            # см. MeOut: адрес из AD не обязан проходить строгую валидацию
    login: str | None = None
    avatar: str | None = None
    full_name: str
    positions: list[PositionOut] = []   # должности-роли из справочника
    phone: str | None = None
    birth_date: date | None = None
    role: Role
    is_active: bool
    is_deleted: bool = False
    password_must_change: bool = False
    incoming_journal_access: bool = False
    movement_access: bool = False
    equipment_access: bool = False
    notes_full_access: bool = False
    substitutions_access: bool = False
    helpdesk_access: bool = False
    created_at: datetime
    last_login_at: datetime | None = None
    auth_source: str = "local"          # local | ldap
    domain_account: str | None = None
    ad_groups: list[Any] | None = None
    last_ad_sync_at: datetime | None = None
    totp_enabled: bool = False          # ПЭП: привязан ли аутентификатор (без деталей секрета)

    # --- «Локдаун»: персональные данные подменяются метками для всех, кроме Владельца ---
    @field_serializer("full_name")
    def _mask_full_name(self, v: str) -> str:
        from app.services import lockdown

        return lockdown.mask_employee(str(self.id)) if lockdown.masking() else v

    @field_serializer("phone", "email", "login", "birth_date", "avatar", "domain_account")
    def _mask_private(self, v):
        from app.services import lockdown

        return None if lockdown.masking() else v


class UserCreateIn(BaseModel):
    email: EmailStr | None = None       # необязателен: без e-mail вход идёт по сгенерированному логину
    login: str | None = Field(default=None, min_length=2, max_length=120,
                              description="Свой логин; если не задан и нет e-mail — генерируется из ФИО")
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    role: Role = Role.TEACHER
    position_ids: list[uuid.UUID] = Field(default_factory=list, description="Должности из справочника")
    phone: str | None = Field(default=None, max_length=40)
    birth_date: date | None = Field(default=None, description="Дата рождения (необязательно)")


class UserUpdateIn(BaseModel):
    """PATCH-семантика: None = не менять (password отдельно)."""

    login: str | None = Field(default=None, min_length=2, max_length=120,
                              description="Смена логина — только Владелец")
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    position_ids: list[uuid.UUID] | None = Field(default=None, description="Должности из справочника")
    phone: str | None = Field(default=None, max_length=40)
    birth_date: date | None = None
    email: EmailStr | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    password_must_change: bool | None = None   # установить может только Владелец
    incoming_journal_access: bool | None = None  # грант журнала входящих — только Владелец
    movement_access: bool | None = None          # грант раздела «Движение» — только Владелец
    equipment_access: bool | None = None         # грант «Учёт оборудования» — только Владелец
    notes_full_access: bool | None = None        # грант «все служебные записки» — только Владелец
    substitutions_access: bool | None = None     # грант «кадровик»: журнал замещений + удаление строк
    helpdesk_access: bool | None = None          # грант «системный администратор» (Helpdesk) — только Владелец


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# ----------------------------------------------------------------------- AD
class LoginOptionsOut(BaseModel):
    """Публичная информация о способах входа (для экрана логина)."""

    ldap_enabled: bool = False
    local_enabled: bool = True
    login_hint: str = ""
    password_managed_by: str = "SchoolHub"


class ADUserOut(BaseModel):
    """Пользователь домена, найденный в каталоге (для импорта)."""

    username: str
    full_name: str
    email: str
    position: str | None = None
    phone: str | None = None
    dn: str | None = None
    disabled: bool = False
    groups: list[str] = []


class ADImportIn(BaseModel):
    usernames: list[str] = Field(min_length=1, max_length=200)
    role: Role | None = None


class ADImportOut(BaseModel):
    created: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    total: int = 0


# ------------------------------------------------------------------ bulk
class UserBulkRowIn(BaseModel):
    """Строка таблицы массового заведения: ФИО + пароль (+ необязательные e-mail и роль)."""

    full_name: str = Field(min_length=2, max_length=255)
    # длину пароля проверяем ПОСТРОЧНО в обработчике (иначе одна плохая строка
    # завернёт весь пакет через 422) — здесь только защита от пустых/огромных значений
    password: str = Field(min_length=1, max_length=128)
    email: EmailStr | None = None
    role: Role = Role.TEACHER
    position: str | None = Field(default=None, max_length=255)


class UserBulkIn(BaseModel):
    rows: list[UserBulkRowIn] = Field(min_length=1, max_length=200,  # bcrypt — операция небыстрая, 200 за раз достаточно
                                      description='Строки таблицы: ФИО, пароль[, e-mail[, роль]]')


class UserBulkCreatedOut(BaseModel):
    full_name: str
    login: str | None = None
    email: str | None = None
    role: Role


class UserBulkFailedOut(BaseModel):
    row: int
    full_name: str
    error: str


class UserBulkOut(BaseModel):
    created: list[UserBulkCreatedOut] = []
    failed: list[UserBulkFailedOut] = []
    total: int = 0


# ------------------------------------------------------------------ ПЭП (TOTP)
class TotpConfirmIn(BaseModel):
    """Подтверждение привязки ПЕРВЫМ кодом из приложения-аутентификатора."""

    code: str = Field(min_length=6, max_length=8, pattern=r"^\d{6}$")


class TotpWordIn(BaseModel):
    """Установка контрольного слова для уровня подписи «код + слово»."""

    word: str = Field(min_length=4, max_length=120)
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d{6}$")


class TotpCodeWordIn(BaseModel):
    """Чувствительные операции (восстановление из копии, очистка аудита,
    локдаун, удаление пользователя) требуют код + контрольное слово.

    Оба поля опциональны НА УРОВНЕ СХЕМЫ: отсутствие подписи — это не ошибка
    формата запроса, а состояние «операция требует усиленную ПЭП», поэтому
    блокировку формулирует сервисный слой как 409 с понятной подсказкой
    (TOTP_NOT_BOUND / RECOVERY_WORD_MISSING), а не как 422 валидации.
    Неверный/просроченный код или неверное слово остаются 422.
    """

    code: str | None = Field(default=None, description="Одноразовый код аутентификатора (6 цифр)")
    word: str | None = Field(default=None, max_length=120, description="Контрольное слово")

    # Формат кода (6 цифр) и слова проверяет сервисный слой (app/services/totp.py),
    # чтобы ошибка была с понятным кодом (TOTP_INVALID_CODE / RECOVERY_WORD_MISMATCH),
    # а не со схемой валидации FastAPI. Здесь поля остаются мягкими.


class TotpDeleteIn(TotpCodeWordIn):
    """Тело DELETE /users/{id} — чувствительная операция ПЭП «код + слово».

    Наследует мягкую валидацию TotpCodeWordIn (см. комментарий там же)."""


class TotpRemoveIn(TotpCodeWordIn):
    """Тело DELETE /users/me/totp — отвязка СВОЕГО аутентификатора.

    Действующая привязка снимается только текущим кодом из приложения
    (контрольное слово не требуется). Отсутствие кода — не 422, а 409
    TOTP_CODE_REQUIRED с подсказкой про сброс Владельцем: так же, как
    в TotpCodeWordIn, состояние «подпись не предоставлена» формулирует
    сервисный слой понятными словами."""


class TotpResetIn(TotpCodeWordIn):
    """Тело POST /users/{id}/totp-reset — сброс чужого аутентификатора Владельцем.

    Операция чувствительная (снимает с сотрудника электронную подпись), поэтому
    требует от Владельца ПЭП максимального уровня «код + контрольное слово»."""


class TotpBreakGlassIn(BaseModel):
    """Тело POST /users/me/totp/break-glass — РЕЗЕРВНЫЙ сброс привязки Владельца (v1.12).

    Единственный путь, которым Владелец восстанавливает электронную подпись,
    потеряв устройство с аутентификатором: код TOTP предъявить нечем, поэтому
    подпись заменяется парой «локальный пароль + заранее заданное контрольное
    слово». Оба поля обязательны, длина слова ограничена схемой, а подбор
    троттлится отдельным лимитером (5 попыток / 30 минут).

    ВАЖНО: пароль проверяется только у ЛОКАЛЬНОЙ учётки. Для доменного Владельца
    (auth_source='ldap') операция запрещена — его пароль живёт в Active Directory,
    и принимать его в запросе к SchoolHub означало бы завести второй, никем не
    контролируемый канал проверки доменных учётных данных.
    """

    password: str = Field(min_length=1, max_length=128, description="Пароль учётной записи Владельца")
    word: str = Field(min_length=1, max_length=120, description="Контрольное слово, заданное заранее")
