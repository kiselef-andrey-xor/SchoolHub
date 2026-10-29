"""Режим «Локдаун» (Lockdown): экстренное сокрытие персональных данных.

Владелец одной кнопкой прячет ФИО, телефоны, e-mail, логины, даты рождения и фото
от ВСЕХ ролей кроме Владельца: вместо них интерфейсы отдают обезличенные метки
«Сотрудник N» / «Ученик N» / «Представитель N». Нужно для демонстрации экрана,
проверок и публикаций скриншотов.

Технически:
  * флаг хранится в таблице system_flags (key='lockdown') с TTL-кэшем 15 секунд;
  * зависимость get_current_user ставит contextvar `masking` для запроса
    (True для всех, кроме Владельца, когда флаг включён);
  * pydantic-сериализаторы схем (UserOut, MeOut, MovementOut, AuditOut, OverdueAck,
    BirthdayOut…) читают contextvar и подменяют персональные поля на маски —
    единая точка, поэтому маскируются ВСЕ эндпоинты, сериализующие эти схемы;
  * пути вывода МИМО схем — CSV-экспорты, печатные формы, тексты уведомлений —
    обязаны вызывать `disp()` (см. ниже). Они строятся вручную из ORM-объектов,
    поэтому сериализаторы их не перехватывают;
  * номера меток детерминированы (crc32 от id/имени): один человек = одна метка
    на всех экранах, но восстановить имя по номеру нельзя.
"""
from __future__ import annotations

import contextvars
import re
import time
import uuid
import zlib

from sqlalchemy import select

_MASKED: contextvars.ContextVar[bool] = contextvars.ContextVar("schoolhub_lockdown_mask", default=False)
_CACHE: dict[str, float | bool] = {"ts": 0.0, "value": False}
CACHE_TTL = 15.0
FLAG_KEY = "lockdown"


async def is_enabled(session) -> bool:
    """Значение флага с коротким кэшем: флаг читается на каждый запрос авторизации."""
    now = time.monotonic()
    if now - float(_CACHE["ts"]) < CACHE_TTL:
        return bool(_CACHE["value"])
    from app.models.system_flag import SystemFlag

    row = (
        await session.execute(select(SystemFlag.value).where(SystemFlag.key == FLAG_KEY))
    ).scalar_one_or_none()
    _CACHE["ts"] = now
    _CACHE["value"] = row in ("1", "true", "on")
    return bool(_CACHE["value"])


def invalidate_cache() -> None:
    _CACHE["ts"] = 0.0
    _NAMES["ts"] = 0.0


def set_masking(flag: bool) -> None:
    _MASKED.set(flag)


def masking() -> bool:
    """True в рамках текущего запроса, если данные нужно обезличивать."""
    return _MASKED.get()


def person_no(seed: str, buckets: int = 9973) -> int:
    return zlib.crc32(seed.encode("utf-8")) % buckets + 1


def mask_employee(seed: str) -> str:
    return f"Сотрудник {person_no(seed)}"


def mask_student(seed: str) -> str:
    return f"Ученик {person_no(seed)}"


def mask_guardian(seed: str) -> str:
    return f"Представитель {person_no(seed)}"


# --------------------------------------------------------------------- единая точка
# Pydantic-схемы маскируются своими валидаторами (app/schemas/*). Но CSV-экспорты,
# печатные формы и тексты уведомлений собираются ВРУЧНУЮ из ORM-объектов и мимо схем
# идут — значит contextvar они не читают и персональные данные отдавали как есть
# (review/REVIEW.md §P1-1). `disp()` — единственная функция для всех НЕ-pydantic
# путей вывода: вызывающий код отдаёт ей исходное значение и устойчивый seed,
# а решение о маскировании принимается здесь.
_KINDS = {
    "employee": mask_employee,
    "student": mask_student,
    "guardian": mask_guardian,
}


def disp(value: str | None, seed: str | uuid.UUID | None = None, kind: str = "employee") -> str:
    """Имя для отображения вне Pydantic-схем: настоящее или обезличенное — по локдауну.

    seed — устойчивый идентификатор человека, чтобы метка «Сотрудник N» не менялась
    между экранами и выгрузками:
      * сотрудники  → передавайте `user.id` (так же делает `schemas/user.py::UserOut`);
      * обучающиеся и представители — это свободный текст, не пользователи системы,
        поэтому seed не передавайте: метка построится от самого имени, ровно как в
        `schemas/movement.py` (`mask_student(v)`), и один ребёнок получит один номер.

    kind: "employee" | "student" | "guardian".
    """
    if value is None:
        return ""
    text = str(value)
    if not masking():
        return text
    return _KINDS.get(kind, mask_employee)(str(seed) if seed is not None else text)


# ------------------------------------------------- системные тексты (уведомления)
# Заголовок и текст уведомления, сводки по просрочке и аттестации собираются
# строкой В МОМЕНТ СОБЫТИЯ («Иванов И. И. отметил(а) задачу…») и хранятся в БД
# готовым текстом: сериализатор схемы не знает, какая часть строки — имя.
# Поэтому для таких текстов есть `scrub()`: он заменяет известные ФИО сотрудников
# их метками. Карта «ФИО -> метка» кэшируется на 15 секунд и наполняется только
# при включённом локдауне (в обычном режиме scrub() возвращает текст как есть).
_NAMES: dict[str, float | dict[str, str] | None] = {"ts": 0.0, "map": None}
_NAMES_RE: re.Pattern[str] | None = None


async def prime_names(session) -> None:
    """Наполняет кэш «ФИО -> метка». Вызывается из get_current_user при локдауне."""
    global _NAMES_RE
    now = time.monotonic()
    if _NAMES["map"] is not None and now - float(_NAMES["ts"]) < CACHE_TTL:
        return

    from app.models.user import User

    rows = (await session.execute(select(User.id, User.full_name))).all()
    names = {
        str(full_name).strip(): mask_employee(str(uid))
        for uid, full_name in rows
        if full_name and str(full_name).strip()
    }
    _NAMES["map"] = names
    _NAMES["ts"] = now
    # длинные имена первыми: иначе «Иванов Иван» срежет начало «Иванов Иван Иванович»
    ordered = sorted(names, key=len, reverse=True)
    _NAMES_RE = re.compile("|".join(re.escape(n) for n in ordered)) if ordered else None


def scrub(text: str | None) -> str | None:
    """Обезличивает системный текст (уведомление, сводку) по локдауну текущего запроса."""
    return scrub_forced(text) if masking() else text


def scrub_forced(text: str | None) -> str | None:
    """Обезличивает текст НЕЗАВИСИМО от контекста запроса.

    Нужно WebSocket'у: push-сообщение собирается в момент записи, то есть в контексте
    АВТОРА действия, а решать должен получатель (Владелец видит ФИО, остальные — метки).
    """
    if not text:
        return text
    names = _NAMES["map"]
    if not names or _NAMES_RE is None:
        return text
    return _NAMES_RE.sub(lambda m: names[m.group(0)], str(text))


def names_primed() -> bool:
    """True, если карта имён наполнена (для диагностики и тестов)."""
    return _NAMES["map"] is not None


def disp_or_blank(value: str | None, seed: str | uuid.UUID | None = None,
                  kind: str = "employee") -> str:
    """Как `disp()`, но при пустом значении возвращает прочерк (для печатных форм)."""
    return disp(value, seed, kind) or "—"


# ------------------------------------------------------------- журнал событий
# audit_log.changes хранит снимки полей в момент действия: ФИО, e-mail, телефон,
# логин, обучающегося, представителя. Сериализатор AuditOut маскирует actor_name /
# actor_email / ip_address, но `changes` и производные от него `summary`/`lines`
# оставались настоящими — то есть журнал раскрывал ПДн при включённом локдауне.
#
# Маскируем на ЧТЕНИИ (не на записи): журнал — юридически значимый след, и
# портить его содержимое нельзя. Владелец видит исходные значения.
_PII_KEYS = {
    # сотрудники
    "full_name", "login", "login_hint", "domain_account", "employee", "actor_name",
    "signatory", "resp", "responsibles", "assignees", "user", "groups",
    # контакты
    "email", "phone", "ip", "ip_address",
    # несовершеннолетние и их представители
    "student", "guardian",
}
# Значения этих ключей — не имена, маскировать их было бы ошибкой
# (name = наименование инвентаря, children = число детей в списке, position = должность).

_KIND_BY_KEY = {"student": "student", "guardian": "guardian"}

# Контакты обезличивать меткой «Сотрудник N» бессмысленно (это не имя), а показывать
# их в локдауне нельзя: телефон и e-mail — такие же персональные данные.
# Поэтому вместо значения — нейтральная заглушка: сам факт изменения остаётся виден.
_CONTACT_KEYS = {"email", "phone", "ip", "ip_address"}
CONTACT_PLACEHOLDER = "скрыто"


def mask_changes(changes: dict | None) -> dict | None:
    """Обезличивает снимок полей журнала событий. Без локдауна возвращает как есть.

    Понимает три формы значения:
      * скаляр                      → «Сотрудник N»;
      * diff `{"from": …, "to": …}` → обезличиваются обе стороны;
      * `before` / `after`          → вложенный словарь, обрабатывается рекурсивно.
    """
    if changes is None or not masking():
        return changes

    def one(key: str, value):
        kind = _KIND_BY_KEY.get(key, "employee")
        if value is None:
            return None
        if key in _CONTACT_KEYS:
            return CONTACT_PLACEHOLDER
        if isinstance(value, str):
            return disp(value, value, kind)
        if isinstance(value, (int, float, bool)):
            return value                      # число/флаг имени не раскрывает
        if isinstance(value, (list, tuple)):
            return [one(key, v) for v in value]
        if isinstance(value, dict):
            return {k: one(key, v) for k, v in value.items()}
        return value

    out: dict = {}
    for key, value in changes.items():
        if key in ("before", "after") and isinstance(value, dict):
            # внутри снимка — те же правила: обезличиваются только персональные ключи,
            # иначе «position: Учитель» превращался бы в «Сотрудник N» и журнал терял смысл
            out[key] = {k: (one(k, v) if k in _PII_KEYS else v) for k, v in value.items()}
        elif key in _PII_KEYS:
            out[key] = one(key, value)
        else:
            out[key] = value
    return out


def mask_name_map(names: dict | None) -> dict | None:
    """Обезличивает карту {user_id: ФИО}, которой audit_human подставляет имена в текст."""
    if not names or not masking():
        return names
    return {uid: mask_employee(str(uid)) for uid in names}
