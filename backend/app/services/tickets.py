"""Одноразовые билеты (short-lived one-time tickets).

Зачем: браузер не может приложить заголовок `Authorization` к
  * открываемому в новой вкладке документу (`<a href>`, `window.open`),
  * WebSocket-подключению (API не позволяет ставить произвольные заголовки),
  * GET-запросу, скачивающему файл.

Прежние обходы были небезопасны:
  * `GET /system/backup` и `/system/files-zip` вообще не требовали второго фактора,
    хотя выгружают всю базу и все документы школы (review/REVIEW.md §P1-4);
  * WebSocket принимал access-токен в query-строке — а он живой 30 минут и попадает
    в access-логи nginx (§5.6).

Билет решает обе задачи: он **одноразовый** (удаляется при предъявлении),
**короткоживущий** (десятки секунд), **привязан к цели** (`purpose`) и
**именной** (выдаётся конкретному пользователю). Токен — 256 бит энтропии
(`secrets.token_urlsafe(32)`), хранится только в памяти процесса, поэтому
ни угадать, ни предъявить чужой билет нельзя. Попадание билета в access-лог
nginx не даёт ничего: к моменту, когда лог кто-то прочитает, билета уже нет.

Хранилище — in-memory. Это корректно при `--workers 1`, которое для этого проекта
обязательно (см. docker-compose.yml и docs/DEPLOY-NAS.md). Для нескольких реплик
замените `_STORE` на Redis с тем же интерфейсом (`setex` / `getdel`).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from app.config import settings
from app.errors import AppError, bad_request, forbidden

# Значения `purpose`. Билет, выданный для одной цели, не принимается для другой.
PURPOSE_DB_BACKUP = "system:db-backup"
PURPOSE_FILES_ZIP = "system:files-zip"
PURPOSE_WEBSOCKET = "realtime:ws"

_PURPOSES = {PURPOSE_DB_BACKUP, PURPOSE_FILES_ZIP, PURPOSE_WEBSOCKET}

# TTL по умолчанию для каждой цели, секунд
_DEFAULT_TTL = {
    PURPOSE_DB_BACKUP: 120,     # успеть скопировать ссылку и начать скачивание
    PURPOSE_FILES_ZIP: 120,
    # обмен происходит сразу при подключении; настраивается WS_TICKET_TTL_SEC
    PURPOSE_WEBSOCKET: settings.ws_ticket_ttl_sec,
}


@dataclass(slots=True)
class Ticket:
    token: str
    purpose: str
    user_id: str
    expires_in: int


# token -> (user_id, purpose, expires_at_monotonic)
_STORE: dict[str, tuple[str, str, float]] = {}

# верхняя граница размера хранилища: билеты создаются аутентифицированными
# пользователями, но неограниченный рост от внешнего ввода — плохая идея
_MAX_STORED = 10_000


def _prune_expired(now: float | None = None) -> None:
    """Убирает протухшие билеты. Заодно не даёт хранилищу расти бесконечно."""
    now = now if now is not None else time.monotonic()
    for token in [t for t, (_, _, exp) in _STORE.items() if exp <= now]:
        _STORE.pop(token, None)
    if len(_STORE) > _MAX_STORED:
        # аварийный сброс самого старого: штатно до этого не доходит
        for token in sorted(_STORE, key=lambda t: _STORE[t][2])[: len(_STORE) - _MAX_STORED]:
            _STORE.pop(token, None)


def issue(user_id: str, purpose: str, ttl_sec: int | None = None) -> Ticket:
    """Выпускает одноразовый билет. `purpose` обязан быть одним из PURPOSE_*."""
    if purpose not in _PURPOSES:
        raise bad_request(f"Неизвестная цель билета: {purpose}")
    ttl = int(ttl_sec if ttl_sec is not None else _DEFAULT_TTL.get(purpose, 60))
    if ttl <= 0 or ttl > 600:
        raise bad_request("Срок жизни билета — от 1 до 600 секунд")

    now = time.monotonic()
    _prune_expired(now)
    token = secrets.token_urlsafe(32)
    _STORE[token] = (str(user_id), purpose, now + ttl)
    return Ticket(token=token, purpose=purpose, user_id=str(user_id), expires_in=ttl)


def consume(token: str | None, purpose: str, user_id: str) -> None:
    """Проверяет и НЕЗАМЕДЛИТЕЛЬНО погашает билет.

    Ошибки намеренно разные по коду, но одинаково не раскрывают содержимое хранилища:
      * билета нет/не тот/чужой  → 403 TICKET_INVALID;
      * истёк                    → 403 TICKET_EXPIRED (отличимо, чтобы UI предложил
        подписать заново, а не показывал «доступ запрещён»).
    """
    if not token:
        raise forbidden("Требуется одноразовый билет: сначала подпишите операцию", code="TICKET_MISSING")
    if purpose not in _PURPOSES:
        raise bad_request(f"Неизвестная цель билета: {purpose}")

    now = time.monotonic()
    entry = _STORE.pop(str(token), None)
    if entry is None:
        # либо не существовал, либо уже использован, либо истёк и убран _prune_expired
        _prune_expired(now)
        raise AppError(403, "Билет недействителен или уже использован", code="TICKET_INVALID")

    owner, entry_purpose, expires_at = entry
    if expires_at <= now:
        raise AppError(403, "Срок действия билета истёк — подпишите операцию заново", code="TICKET_EXPIRED")
    if entry_purpose != purpose:
        raise forbidden("Билет выдан для другой операции", code="TICKET_INVALID")
    # билет именной: предъявить чужой нельзя
    if not hmac.compare_digest(owner, str(user_id)):
        raise forbidden("Билет выдан другому пользователю", code="TICKET_INVALID")


def peek(token: str | None, purpose: str) -> str | None:
    """Возвращает user_id по действующему билету БЕЗ погашения.

    Нужен WebSocket'у: соединение живёт долго, а погасить билет надо сразу,
    поэтому там используется `consume()`; `peek()` — для диагностики и тестов.
    """
    if not token:
        return None
    now = time.monotonic()
    entry = _STORE.get(str(token))
    if entry is None:
        return None
    owner, entry_purpose, expires_at = entry
    if entry_purpose != purpose or expires_at <= now:
        return None
    return owner


def stats() -> dict[str, int]:
    """Служебная информация: сколько билетов живо (диагностика и тесты)."""
    _prune_expired()
    return {"active": len(_STORE), "max": _MAX_STORED}


def _fingerprint(token: str) -> str:  # pragma: no cover - отладка
    """Короткий невосстановимый отпечаток для логов (сам билет логировать нельзя)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


__all__ = [
    "PURPOSE_DB_BACKUP",
    "PURPOSE_FILES_ZIP",
    "PURPOSE_WEBSOCKET",
    "Ticket",
    "consume",
    "issue",
    "peek",
    "stats",
]
