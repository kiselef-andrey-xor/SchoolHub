"""Конфигурация приложения (12-factor: всё из ENV)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource, SettingsConfigDict

from app.enums import Role


class _LenientComplexMixin:
    """CORS_ORIGINS принимает и JSON, и перечисление через запятую (v1.10).

    Без этого `CORS_ORIGINS=https://school.ru,https://mir.ru` в .env на NAS
    уронил бы бэкенд на старте: pydantic-settings требует от списков строгий
    JSON ещё до вызова валидаторов поля.
    """

    def decode_complex_value(self, field_name, field, value):  # type: ignore[no-untyped-def]
        if field_name == "cors_origins":
            s = value.strip()
            if not s:
                return []
            if not s.startswith("["):
                return [part.strip() for part in s.split(",") if part.strip()]
        return super().decode_complex_value(field_name, field, value)  # type: ignore[misc]


class _LenientEnvSource(_LenientComplexMixin, EnvSettingsSource):
    pass


class _LenientDotEnvSource(_LenientComplexMixin, DotEnvSettingsSource):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # тот же порядок источников, но с «мягким» разбором CORS_ORIGINS
        return (
            init_settings,
            _LenientEnvSource(settings_cls),
            _LenientDotEnvSource(settings_cls),
            file_secret_settings,
        )

    # --- app ---
    app_name: str = "SchoolHub API"
    app_env: str = "dev"                     # dev | staging | prod
    api_prefix: str = "/api/v1"
    # `debug` здесь намеренно НЕТ: параметр ни на что не влиял (документация отключается
    # по APP_ENV=prod), а README советовал выставить DEBUG=false — получалось расхождение
    # «настройка есть, но ничего не делает» (review/REVIEW.md §6.4).

    # --- db ---
    database_url: str = "postgresql+asyncpg://schoolhub:schoolhub@localhost:5432/schoolhub"
    db_echo: bool = False
    # применять Alembic-миграции при старте приложения (на NAS — обязательно true;
    # в крупных контурах можно выключить и накатывать миграции вручную: alembic upgrade head)
    auto_migrate: bool = True

    # --- security ---
    # SECRET_KEY можно не задавать: при старте сгенерируется и сохранится в файл
    # (см. app/services/bootstrap.py::resolve_secret_key). Файл лежит в постоянном
    # каталоге данных, поэтому ключ переживает пересоздание контейнера.
    secret_key: str = Field(default="")
    secret_key_file: str = "./storage/secret_key"
    setup_token: str = ""                  # задать свой код настройки (иначе сгенерируется)
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 14
    password_min_length: int = 8

    # --- refresh-cookie (httpOnly вместо localStorage) ---
    refresh_cookie_name: str = "schoolhub_refresh"
    # `auto` (по умолчанию) — включать флаг Secure автоматически, когда запрос пришёл
    # по HTTPS или за TLS-терминатором (X-Forwarded-Proto: https). Явно задаётся
    # COOKIE_SECURE=true/false. См. app/services/sessions.py::cookie_options.
    cookie_secure: bool | str = "auto"
    cookie_same_site: str = "strict"       # strict | lax — защита от CSRF на /auth/refresh
    # Льготное окно ротации refresh-токена для параллельных вкладок, секунд.
    # В пределах окна повторное предъявление заменённого токена не считается кражей
    # (иначе две вкладки, обновившие страницу одновременно, роняли бы сессию).
    # Держим коротким: реальная параллельная вкладка укладывается в сотни миллисекунд,
    # а каждая лишняя секунда — шанс для атакующего предъявить украденный токен
    # и получить валидную пару вместо сигнала reuse_detected (review/REVIEW.md §5.9).
    refresh_reuse_grace_sec: int = 10

    # --- files ---
    upload_dir: str = "./storage/uploads"
    max_upload_mb: int = 25
    allowed_doc_mimes: List[str] = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ]
    # Конвертация DOCX/DOC → PDF для инлайн-превью (LibreOffice headless).
    # Таймаут на ОДИН документ: по его истечении отдаём оригинал на скачивание.
    # На DS223 (2 ГБ RAM, ARMv8) холодный soffice медленный — при проблемах поднимайте.
    doc_convert_timeout_sec: int = 60

    # --- аварийный сброс аутентификатора Владельца (v1.12) ---
    # Потерял телефон — привязку можно снять паролем + заранее заданным
    # контрольным словом. Лимит намеренно узкий: операция снимает электронную
    # подпись Владельца и открывает доступ к восстановлению из копии.
    break_glass_max_attempts: int = 5
    break_glass_window_min: int = 30

    # --- Helpdesk (v1.12) ---
    # Через сколько дней после выполнения заявка закрывается автоматически,
    # если автор так и не подтвердил и не вернул её на доработку.
    ticket_autoclose_days: int = 5

    # Вложения задач — шире, чем документы приказов: офисные файлы, изображения, архивы.
    allowed_task_file_mimes: List[str] = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/zip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
    ]

    # --- Active Directory / LDAP -------------------------------------------------
    # Аутентификация bind'ом под учёткой пользователя (пароль в БД не хранится).
    ldap_enabled: bool = False
    ldap_server: str = ""                    # dc01.school.local:636  или  ldaps://dc01.school.local
    ldap_use_ssl: bool = True                # true = LDAPS(636); false = LDAP(389) + StartTLS
    ldap_use_starttls: bool = True           # применяется только при ldap_use_ssl=false
    ldap_tls_verify: str = "required"        # required | none  (none — только для отладки!)
    ldap_ca_cert: str = ""                   # путь к PEM-файлу с корневым CA домена (если самоподписанный)
    ldap_bind_template: str = "{username}@{domain}"   # UPN; альтернатива: "{domain}\\{username}"
    ldap_domain: str = ""                    # SCHOOL.LOCAL — подставляется в шаблон и как суффикс e-mail
    ldap_base_dn: str = ""                   # DC=school,DC=local
    ldap_user_filter: str = "(&(objectClass=user)(objectCategory=person))"
    ldap_group_filter: str = "(objectClass=group)"
    # Сервисная учётка — ТОЛЬКО для чтения (поиск пользователя, групп, импорт из AD).
    # Для самого bind'а используется пароль, введённый пользователем.
    ldap_bind_dn: str = ""                   # CN=svc_schoolhub,OU=Service,DC=school,DC=local
    ldap_bind_password: str = ""
    ldap_login_format: str = "upn"           # upn | downlevel | both
    ldap_provisioning: str = "auto"          # auto = создавать учётку при первом входе | manual
    ldap_default_role: Role = Role.TEACHER   # роль нового пользователя из домена
    ldap_allowed_groups: List[str] = []      # пусто = разрешён весь домен; иначе whitelist групп (sAMAccountName или DN)
    ldap_admin_groups: List[str] = []        # группы AD, участники которых получают роль ADMIN
    ldap_owner_groups: List[str] = []        # группы AD для роли OWNER (используйте крайне осторожно)
    ldap_attr_map: dict = {                  # соответствие атрибутов AD → поля SchoolHub
        "username": "sAMAccountName",
        "upn": "userPrincipalName",
        "email": "mail",
        "full_name": "displayName",
        "first_name": "givenName",
        "last_name": "sn",
        "position": "title",
        "phone": "telephoneNumber",
        "groups": "memberOf",
    }
    ldap_timeout_sec: int = 10
    ldap_sync_groups_on_login: bool = True   # обновлять список групп AD при каждом входе
    ldap_sync_attrs_on_login: bool = True    # подтягивать ФИО/должность/телефон/e-mail из AD при входе
    ldap_role_from_groups: str = "upgrade"   # upgrade = группы AD могут только повышать роль; off = роли только вручную
    ldap_resolve_group_sids: bool = False    # резолвить SID групп (tokenGroups) в имена — даёт вложенные группы

    # --- защита от перебора (важно: чужой брутфорс может заблокировать доменные учётки) ---
    # --- напоминания об ознакомлении (фоновый цикл в контейнере) ---
    reminders_enabled: bool = True           # REMINDERS_ENABLED=false — выключить
    reminder_days_before: int = 3              # за сколько дней до дедлайна напоминать

    # --- housekeeping (self-healing): автоочистка сессий и архивация раздутого журнала ---
    housekeeping_enabled: bool = True          # HOUSEKEEPING_ENABLED=false — выключить
    session_purge_after_days: int = 30         # истёкшие старше N дней удаляются из refresh_sessions
    audit_archive_threshold: int = 300_000     # строк audit_log, после которых включается архивация
    audit_keep_rows: int = 150_000             # сколько свежих строк оставлять в БД после архивации

    # --- архив файлов школы (Профиль → «Резервное копирование» → «Файлы школы») ---
    # Архив «Файлы школы». Размер ПРИНИМАЕМОГО ZIP ограничен сверху значением,
    # совместимым с лимитом памяти контейнера (deploy.resources.limits.memory: 460M):
    # архив читается потоково на диск, поэтому в память не загружается целиком,
    # но 2 ГБ на 2 ГБ NAS — это всё равно риск. Суммарный объём РАСПАКОВАННОГО
    # ограничен отдельно (защита от zip-bomb: много членов по ~100 МБ).
    files_archive_max_mb: int = 512
    files_archive_unpacked_max_mb: int = 4096
    reminder_hour: int = 9                     # час ежедневной сводки администрации по просрочке
    # за сколько дней предупреждать об истечении аттестации / срока КПК
    cert_reminder_days: int = 60

    login_rate_limit: int = 10               # попыток
    login_rate_window_min: int = 15          # за период

    # --- защита одноразового кода подписи (ПЭП) от перебора ---
    # Код TOTP — 6 цифр, а с окном ±1 шаг валидны сразу три значения: без троттлинга
    # код подбирается перебором за несколько часов. После исчерпания попыток проверка
    # кода отвечает 429, а каждая неудача пишется в журнал событий (SIGNATURE_FAILED).
    totp_max_attempts: int = 5               # неудачных проверок кода/контрольного слова
    totp_lock_window_min: int = 15           # за период; 0 — отключить троттлинг подписи

    # --- WebSocket-уведомления ---
    # Браузерный WebSocket не умеет ставить заголовки, поэтому access-токен в
    # query-строке попадал в логи nginx. Вместо него выдаётся одноразовый билет
    # (см. app/api/realtime.py). TTL короткий: билет обменивается сразу при подключении.
    ws_ticket_ttl_sec: int = 60

    # --- misc ---
    cors_origins: List[str] = ["http://localhost:5173", "http://localhost:4173"]
    auto_seed: bool = False                  # засеять демо-данные при старте (только dev)
    trust_proxy_headers: bool = False        # брать IP клиента из X-Forwarded-For (за nginx/DSM reverse proxy)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        """CORS_ORIGINS из .env: JSON-список ИЛИ перечисление через запятую.

        `https://a.ru,https://b.ru` без JSON-кавычек — частая ошибка в .env на NAS;
        «в лоб» pydantic уронил бы старт бэкенда. Пустое значение — список без
        внешних origin (SPA работает same-origin через nginx фронтенда).
        """
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                return v  # JSON — пусть pydantic разбирает сам
            return [part.strip() for part in s.split(",") if part.strip()]
        return v

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    # --- производные значения LDAP ---
    @property
    def ldap_port(self) -> int:
        """Порт из ldap_server (или дефолт по признаку SSL)."""
        if ":" in self.ldap_server and "//" not in self.ldap_server.split(":")[0]:
            try:
                return int(self.ldap_server.rsplit(":", 1)[1])
            except ValueError:
                pass
        return 636 if self.ldap_use_ssl else 389

    @property
    def ldap_host(self) -> str:
        host = self.ldap_server.split("//")[-1].split("/")[0]
        return host.rsplit(":", 1)[0] if ":" in host else host


def _resolve_secret_key(cfg: Settings) -> str:
    """Ключ подписи JWT: ENV → файл → автогенерация.

    Автогенерация делает установку «без секретов»: администратору не нужно ничего
    придумывать, а ключ сохраняется в постоянном каталоге данных и переживает
    пересоздание контейнера (сессии пользователей не сбрасываются).
    """
    import logging
    import secrets
    from pathlib import Path

    log = logging.getLogger("schoolhub.config")

    env_key = (os.getenv("SECRET_KEY") or "").strip()
    if env_key:
        if len(env_key) < 32:
            raise RuntimeError("SECRET_KEY короче 32 символов — задайте надёжнее или удалите переменную")
        return env_key

    key_file = Path(cfg.secret_key_file).expanduser()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            stored = key_file.read_text(encoding="utf-8").strip()
            if len(stored) >= 32:
                return stored
            # короткий ключ непригоден (HS256 требует ≥256 бит): заменяем, но говорим
            # вслух — это сбрасывает все сессии, и администратор должен понимать причину
            log.warning(
                "Файл %s содержит ключ короче 32 символов — генерируется новый. "
                "Все сессии будут сброшены (пользователям придётся войти заново)",
                key_file,
            )
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:  # pragma: no cover
            pass
        log.info("SECRET_KEY сгенерирован автоматически и сохранён: %s", key_file)
        return generated
    except OSError as exc:  # pragma: no cover
        log.warning("Не удалось сохранить SECRET_KEY (%s) — используется временный", exc)
        return secrets.token_urlsafe(48)


@lru_cache
def get_settings() -> Settings:
    cfg = Settings()
    resolved = _resolve_secret_key(cfg)
    if resolved != cfg.secret_key:
        object.__setattr__(cfg, "secret_key", resolved)
    return cfg


settings = get_settings()
