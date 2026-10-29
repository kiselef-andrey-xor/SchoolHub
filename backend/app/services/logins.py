"""Генерация логинов локальных учётных записей без e-mail.

Школа может завести учётку «ФИО + пароль» (в том числе массовой загрузкой).
Вход в систему тогда идёт по логину, который генерируется из ФИО транслитерацией:
    «Иванова Иванна Петровна» -> ivanova.ivanna
    «Ли Алексей»               -> li.aleksey
При коллизии добавляется числовой суффикс: ivanova.ivanna2, ivanova.ivanna3…

Логин состоит только из символов, допустимых схемой входа (LOGIN_RE в schemas/user.py):
латиница, цифры, точка, подчёркивание, дефис.
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(value: str) -> str:
    """Текст -> латиница/цифры/точки: кириллица транслитом, остальное NFKD-раскладкой."""
    out: list[str] = []
    for ch in value.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        elif ch in " ._,;:/\\":
            out.append(" ")
        else:
            decomposed = unicodedata.normalize("NFKD", ch)
            ascii_part = "".join(c for c in decomposed if c.isascii() and c.isalnum())
            out.append(ascii_part if ascii_part else " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def base_login(full_name: str) -> str:
    """'Иванова Иванна Петровна' -> 'ivanova.ivanna' (фамилия + имя)."""
    words = [w for w in transliterate(full_name).replace(".", " ").replace("-", " ").split() if w]
    if not words:
        return ""
    if len(words) == 1:
        base = words[0]
    else:
        base = f"{words[0]}.{words[1]}"
    base = re.sub(r"[^a-z0-9._-]", "", base)
    return base[:60] or ""


async def generate_login(session: AsyncSession, full_name: str, taken: set[str] | None = None) -> str:
    """Уникальный логин для ФИО: база + суффикс при коллизии (в БД и в наборе `taken`).

    `taken` — логины, занятые другими строками того же массового запроса
    (ещё не закоммичены/не заflush-ены, поэтому в БД их может не быть).
    """
    base = base_login(full_name) or "user"
    candidate = base
    suffix = 1
    while True:
        exists = (
            await session.execute(select(User.id).where(User.login == candidate).limit(1))
        ).first()
        if exists is None and (taken is None or candidate not in taken):
            return candidate
        suffix += 1
        candidate = f"{base}{suffix}"
        if suffix > 999:  # защита от бесконечного цикла при патологических данных
            import uuid as _uuid

            return f"{base}-{_uuid.uuid4().hex[:6]}"
