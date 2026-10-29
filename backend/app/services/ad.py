"""Интеграция с Active Directory / LDAP.

Модель аутентификации — **bind под учёткой пользователя**: пароль проверяет сам
контроллер домена, мы его нигде не храним и не логируем.

Два подключения:
  1. *сервисное* (read-only учётка `LDAP_BIND_DN`) — чтобы найти DN пользователя по
     логину и прочитать его атрибуты/группы;
  2. *пользовательское* — bind с введённым паролом: успешный bind = успешная аутентификация.

Все сетевые вызовы ldap3 синхронные, поэтому выполняются в пуле потоков
(`asyncio.to_thread`), чтобы не блокировать event loop FastAPI.

Логика отделена от транспорта интерфейсом `ADProvider` — это позволяет
тестировать сценарии входа на заглушке без реального контроллера домена.
"""
from __future__ import annotations

import asyncio
import logging
import ssl as ssl_module
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.config import settings
from app.enums import Role
from app.errors import AppError, forbidden, unauthorized

logger = logging.getLogger("schoolhub.ad")

#well-known SID'ы встроенных групп AD (нужны, если резолвим tokenGroups)
WELL_KNOWN_SIDS = {
    "S-1-5-32-544": "Administrators",
    "S-1-5-32-545": "Users",
    "S-1-5-32-546": "Guests",
}

# коды ошибок AD → понятное сообщение (важно: не раскрывать, существует ли учётка)
AD_ERROR_HINTS = {
    "525": "Пользователь не найден в каталоге",
    "52e": "Неверное имя пользователя или пароль",
    "530": "Вход с этой рабочей станции запрещён",
    "531": "Вход разрешён только с компьютеров домена",
    "532": "Срок действия пароля истёк",
    "533": "Учётная запись заблокирована администратором домена",
    "701": "Срок действия учётной записи истёк",
    "773": "Требуется смена пароля (обратитесь к администратору домена)",
    "775": "Учётная запись заблокирована из-за превышения числа попыток входа",
}


class ADError(AppError):
    """Ошибка интеграции с каталогом (недоступен DC, нет сервисной учётки и т.п.)."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(status_code, message, code="AD_ERROR")


@dataclass(slots=True)
class ADPrincipal:
    """Пользователь домена, возвращаемый провайдером."""

    username: str                       # sAMAccountName
    dn: str = ""
    upn: str = ""
    email: str = ""
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    phone: str = ""
    object_sid: str = ""
    groups: list[str] = field(default_factory=list)      # sAMAccountName групп (или DN, если имя не резолвилось)
    group_dns: list[str] = field(default_factory=list)   # memberOf как есть
    disabled: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class ADProvider(ABC):
    """Интерфейс провайдера каталога (для тестов подменяется заглушкой)."""

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> ADPrincipal:
        """Проверить учётные данные домена и вернуть профиль пользователя."""

    @abstractmethod
    async def search_users(self, query: str, limit: int = 50) -> list[ADPrincipal]:
        """Поиск по каталогу (для «Импорта из AD» администратором)."""

    @abstractmethod
    async def ping(self) -> dict[str, Any]:
        """Диагностика соединения."""


# --------------------------------------------------------------------- LDAP
class LdapADProvider(ADProvider):
    """Реализация на ldap3 (чистый Python — без проблем собирается под linux/arm64)."""

    def __init__(self) -> None:
        self.map = settings.ldap_attr_map

    # ------------------------------------------------------------ connection
    def _tls(self):
        from ldap3 import Tls

        if settings.ldap_tls_verify == "none":
            # Сертификат контроллера не проверяется: в сети школы возможен перехват
            # bind-пароля (это доменный пароль сотрудника). Режим оставлен только для
            # отладки, поэтому говорим о нём в журнал при каждом соединении.
            logger.warning(
                "LDAP_TLS_VERIFY=none — сертификат домена НЕ проверяется. "
                "Соединение уязвимо к подмене сервера и перехвату пароля; "
                "для боя задайте LDAP_TLS_VERIFY=required и, при самоподписанном CA, "
                "LDAP_CA_CERT=/путь/к/ca.pem"
            )
            return Tls(validate=ssl_module.CERT_NONE)
        kwargs: dict[str, Any] = {"validate": ssl_module.CERT_REQUIRED}
        if settings.ldap_ca_cert:
            kwargs["ca_certs_file"] = settings.ldap_ca_cert
        return Tls(**kwargs)

    def _server(self):
        from ldap3 import Server

        return Server(
            settings.ldap_host,
            port=settings.ldap_port,
            use_ssl=settings.ldap_use_ssl,
            tls=None if settings.ldap_use_ssl else self._tls(),
            connect_timeout=settings.ldap_timeout_sec,
            get_info="NONE",
        )

    def _connect(self, user: str | None, password: str | None):
        """Создаёт соединение и выполняет bind. Бросает ADError с понятной причиной."""
        from ldap3 import SYNC, Connection
        from ldap3.core.exceptions import LDAPException

        try:
            conn = Connection(
                self._server(),
                user=user,
                password=password,
                auto_bind=False,
                receive_timeout=settings.ldap_timeout_sec,
                client_strategy=SYNC,
                raise_exceptions=True,
            )
            if not settings.ldap_use_ssl and settings.ldap_use_starttls:
                conn.start_tls()
            conn.bind()
            return conn
        except LDAPException as exc:
            detail = _describe_ldap_error(exc)
            logger.warning("LDAP bind/connect failed: %s", detail)
            raise ADError(detail) from exc

    def _service_conn(self):
        if not settings.ldap_bind_dn:
            raise ADError(
                "Не задана сервисная учётка LDAP_BIND_DN — поиск пользователя в каталоге невозможен"
            )
        return self._connect(settings.ldap_bind_dn, settings.ldap_bind_password)

    # ---------------------------------------------------------------- search
    def _build_filter(self, query: str) -> str:
        from ldap3.utils.conv import escape_filter_chars

        safe = escape_filter_chars(query.strip())
        attr = self.map.get("username", "sAMAccountName")
        upn_attr = self.map.get("upn", "userPrincipalName")
        base = settings.ldap_user_filter or "(objectClass=user)"

        variants = [f"({attr}={safe})", f"({upn_attr}={safe})"]
        if settings.ldap_domain:
            variants.append(f"({upn_attr}={safe}@{escape_filter_chars(settings.ldap_domain)})")
            variants.append(f"({attr}={safe})")
        or_part = "".join(dict.fromkeys(variants))
        return f"(&{base}(|{or_part}))"

    def _attributes(self) -> list[str]:
        m = self.map
        attrs = {
            m.get("username", "sAMAccountName"),
            m.get("upn", "userPrincipalName"),
            m.get("email", "mail"),
            m.get("full_name", "displayName"),
            m.get("first_name", "givenName"),
            m.get("last_name", "sn"),
            m.get("position", "title"),
            m.get("phone", "telephoneNumber"),
            m.get("groups", "memberOf"),
            "objectGUID",
            "userAccountControl",
        }
        if settings.ldap_resolve_group_sids:
            attrs.add("tokenGroups")
        return sorted(a for a in attrs if a)

    def _search(self, conn, ldap_filter: str, limit: int) -> list[Any]:
        conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=ldap_filter,
            search_scope="SUBTREE",
            attributes=self._attributes(),
            size_limit=limit,
        )
        return list(conn.entries)

    def _to_principal(self, entry: Any) -> ADPrincipal:
        m = self.map
        attrs = entry.entry_attributes_as_dict if hasattr(entry, "entry_attributes_as_dict") else {}

        def one(key: str, default: str = "") -> str:
            name = m.get(key, key)
            values = attrs.get(name) or []
            return str(values[0]) if values else default

        def many(key: str) -> list[str]:
            name = m.get(key, key)
            return [str(v) for v in (attrs.get(name) or [])]

        group_dns = many("groups")
        username = one("username") or (one("upn").split("@")[0] if one("upn") else "")
        disabled = _is_disabled(attrs.get("userAccountControl", [0])[0])

        principal = ADPrincipal(
            username=username,
            dn=str(entry.entry_dn),
            upn=one("upn"),
            email=one("email"),
            full_name=one("full_name"),
            first_name=one("first_name"),
            last_name=one("last_name"),
            position=one("position"),
            phone=one("phone"),
            group_dns=group_dns,
            groups=[_cn_from_dn(dn) for dn in group_dns],
            disabled=disabled,
            raw={k: [str(v) for v in vals] for k, vals in attrs.items()},
        )
        if not principal.full_name:
            principal.full_name = " ".join(x for x in (principal.last_name, principal.first_name) if x) or username
        if not principal.email:
            principal.email = (
                principal.upn
                or (f"{username}@{settings.ldap_domain.lower()}" if settings.ldap_domain else "")
            )

        if settings.ldap_resolve_group_sids:
            sids = attrs.get("tokenGroups") or []
            resolved = _resolve_group_sids([_sid_to_str(sid) for sid in sids])
            if resolved:
                principal.groups = sorted(set(principal.groups) | set(resolved))
        return principal

    # ------------------------------------------------------------ public api
    async def authenticate(self, username: str, password: str) -> ADPrincipal:
        if not username or not password:
            raise unauthorized("Неверное имя пользователя или пароль")

        def _work() -> ADPrincipal:
            # 1) сервисное подключение: ищем DN пользователя
            svc = self._service_conn()
            try:
                entries = self._search(svc, self._build_filter(username), limit=2)
                if not entries:
                    logger.info("AD: пользователь '%s' не найден в каталоге", username)
                    raise unauthorized("Неверное имя пользователя или пароль")
                if len(entries) > 1:
                    logger.warning("AD: найдено несколько записей для '%s' — берём первую", username)
                entry = entries[0]
                user_dn = str(entry.entry_dn)
                principal = self._to_principal(entry)
            finally:
                svc.unbind()

            # 2) bind под учёткой пользователя — это и есть проверка пароля доменом
            user_conn = self._connect(user_dn, password)
            user_conn.unbind()

            if principal.disabled:
                raise forbidden("Учётная запись в домене отключена")
            return principal

        return await asyncio.to_thread(_work)

    async def search_users(self, query: str, limit: int = 50) -> list[ADPrincipal]:
        if not query.strip():
            return []

        def _work() -> list[ADPrincipal]:
            from ldap3.utils.conv import escape_filter_chars

            svc = self._service_conn()
            try:
                safe = escape_filter_chars(query.strip())
                m = self.map
                flt = (
                    f"(&{settings.ldap_user_filter or '(objectClass=user)'}"
                    f"(|({m.get('username','sAMAccountName')}=*{safe}*)"
                    f"({m.get('full_name','displayName')}=*{safe}*)"
                    f"({m.get('email','mail')}=*{safe}*)))"
                )
                return [self._to_principal(e) for e in self._search(svc, flt, limit)]
            finally:
                svc.unbind()

        return await asyncio.to_thread(_work)

    async def ping(self) -> dict[str, Any]:
        def _work() -> dict[str, Any]:
            svc = self._service_conn()
            try:
                svc.search(
                    search_base=settings.ldap_base_dn,
                    search_filter="(objectClass=*)",
                    search_scope="BASE",
                    attributes=["defaultNamingContext", "dnsHostName"],
                    size_limit=1,
                )
                info: dict[str, Any] = {}
                if svc.entries:
                    info = {
                        str(k): [str(v) for v in vals]
                        for k, vals in svc.entries[0].entry_attributes_as_dict.items()
                    }
                return {
                    "ok": True,
                    "server": f"{settings.ldap_host}:{settings.ldap_port}",
                    "ssl": settings.ldap_use_ssl,
                    "starttls": (not settings.ldap_use_ssl) and settings.ldap_use_starttls,
                    "base_dn": settings.ldap_base_dn,
                    "root": info,
                }
            finally:
                svc.unbind()

        try:
            return await asyncio.to_thread(_work)
        except ADError as exc:
            return {"ok": False, "error": exc.detail, "server": f"{settings.ldap_host}:{settings.ldap_port}"}


# ------------------------------------------------------------------ helpers
def _cn_from_dn(dn: str) -> str:
    """'CN=Учителя,OU=Groups,DC=school,DC=local' -> 'Учителя'."""
    for part in dn.split(","):
        part = part.strip()
        if part.upper().startswith("CN="):
            return part[3:]
    return dn


def _is_disabled(uac: Any) -> bool:
    """Флаг ACCOUNTDISABLE (0x0002) в userAccountControl."""
    try:
        return bool(int(uac) & 0x0002)
    except (TypeError, ValueError):
        return False


def _sid_to_str(raw: Any) -> str:
    """Бинарный SID → строковое представление."""
    if isinstance(raw, str):
        return raw
    try:
        data = bytes(raw)
        revision = data[0]
        sub_count = data[1]
        authority = int.from_bytes(data[2:8], "big")
        parts = [f"S-{revision}-{authority}"]
        for i in range(sub_count):
            offset = 8 + i * 4
            parts.append(str(int.from_bytes(data[offset : offset + 4], "little")))
        return "-".join(parts)
    except Exception:  # pragma: no cover
        return str(raw)


def _resolve_group_sids(sids: Iterable[str]) -> list[str]:
    """SID'ы групп → имена (по таблице well-known SID; для остальных имён нужен запрос в каталог)."""
    return [WELL_KNOWN_SIDS[sid] for sid in sids if sid in WELL_KNOWN_SIDS]


def _describe_ldap_error(exc: Exception) -> str:
    """Человеческое описание ошибки LDAP/AD (включая коды 52e/775 из bind-ответа)."""
    text = str(exc)
    lowered = text.lower()
    if "data" in lowered:
        for code, hint in AD_ERROR_HINTS.items():
            if f"data {code}" in lowered or f"-{code}" in lowered:
                return f"{hint} (код AD {code})"
    if "socket" in lowered or "timed out" in lowered or "unreachable" in lowered or "refused" in lowered:
        return f"Контроллер домена недоступен: {settings.ldap_host}:{settings.ldap_port}"
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return (
            "Ошибка TLS при подключении к домену: проверьте LDAP_TLS_VERIFY, LDAP_CA_CERT "
            "и что сертификат DC выдан на его DNS-имя"
        )
    if "stronger" in lowered or "signing" in lowered:
        return "DC требует LDAP signing — используйте LDAPS (LDAP_USE_SSL=true, порт 636)"
    if "invalid credentials" in lowered:
        return "Сервисная учётка LDAP отклонена контроллером домена"
    return f"Ошибка каталога: {text[:200]}"


def enforce_group_whitelist(principal: ADPrincipal) -> None:
    """Белый список групп AD (если задан). Нарушение — 403 (не 401: учётка существует).

    Вызывается из общего слоя аутентификации (`ad_users.authenticate_principal`),
    а не из конкретного провайдера — иначе замена провайдера молча отключила бы проверку.
    """
    allowed = [g.strip() for g in settings.ldap_allowed_groups if g.strip()]
    if not allowed:
        return
    user_groups = {g.lower() for g in principal.groups} | {dn.lower() for dn in principal.group_dns}
    if not any(g.lower() in user_groups for g in allowed):
        logger.info("AD: пользователь %s не входит в разрешённые группы", principal.username)
        raise forbidden("Доступ в SchoolHub разрешён только сотрудникам школы (нет членства в разрешённых группах AD)")


def role_from_groups(groups: Sequence[str]) -> Role:
    """Роль по членству в группах AD (маппинг задаётся ENV).

    По умолчанию все входящие получают `LDAP_DEFAULT_ROLE` (Учитель) — роли
    раздаёт Владелец в интерфейсе, группы лишь подсказка/опциональное повышение.
    """
    names = {g.lower() for g in groups}
    owners = {g.lower() for g in settings.ldap_owner_groups if g.strip()}
    admins = {g.lower() for g in settings.ldap_admin_groups if g.strip()}
    if owners and names & owners:
        return Role.OWNER
    if admins and names & admins:
        return Role.ADMIN
    return settings.ldap_default_role


# --------------------------------------------------------------- singleton
_provider: ADProvider | None = None


def get_provider() -> ADProvider:
    """Текущий провайдер каталога (в тестах подменяется через set_provider)."""
    global _provider
    if _provider is None:
        _provider = LdapADProvider()
    return _provider


def set_provider(provider: ADProvider | None) -> None:
    global _provider
    _provider = provider


def ad_enabled() -> bool:
    return bool(settings.ldap_enabled and settings.ldap_server and settings.ldap_base_dn)


def normalize_username(raw: str) -> str:
    """Приводит введённый логин к короткому имени: 'DOMAIN\\user', 'user@domain' → 'user'."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "\\" in value:
        value = value.split("\\", 1)[1]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


def build_login_id(principal: ADPrincipal) -> str:
    """Идентификатор доменного пользователя в БД SchoolHub (sAMAccountName, lower)."""
    return (principal.username or normalize_username(principal.upn) or principal.email.split("@")[0]).lower()


def candidate_emails(principal: ADPrincipal) -> list[str]:
    """Варианты e-mail, по которым ищем существующую учётку SchoolHub."""
    out: list[str] = []
    for value in (principal.email, principal.upn):
        if value and value.lower() not in [v.lower() for v in out]:
            out.append(value.lower())
    if settings.ldap_domain and principal.username:
        guess = f"{principal.username.lower()}@{settings.ldap_domain.lower()}"
        if guess not in out:
            out.append(guess)
    return out
