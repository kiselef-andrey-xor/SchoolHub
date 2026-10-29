"""Справочник должностей и сопоставление ответственных из документов/ссылок.

Ссылка ответственного (ref) — строка одного из видов:
  * «<uuid>»      — конкретный сотрудник;
  * «pos:<uuid>»  — должность целиком (все держатели, напр. «классные руководители»);
  * «admin»       — вся администрация (системные роли OWNER и ADMIN);
  * «text:…»      — нераспознанный текст из импортированного документа (только показ).

Набор ключей пользователя (user_resp_keys): его uuid, «pos:<uuid>» каждой его
должности и «admin», если роль OWNER/ADMIN, — по нему мероприятия плана/ВШК
попадают на дашборд сотрудника.
"""
from __future__ import annotations

import re
import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.position import Position
from app.models.user import User

ADMIN_REF = "admin"
ADMIN_WORDS = {"администрация", "администрация школы"}

# Стартовый справочник должностей: (название, синонимы для распознавания в документах)
SEED_POSITIONS: list[tuple[str, str]] = [
    ("Директор", "директор школы"),
    ("Заместитель директора по учебно-воспитательной работе",
     "зам по увр, зам директора по увр, замдиректора по увр, завуч"),
    ("Заместитель директора по воспитательной работе",
     "зам по вр, зам директора по вр, замдиректора по вр"),
    ("Заместитель директора по научно-методической работе",
     "зам по нмр, зам директора по нмр, замдиректора по нмр, нмр"),
    ("Заместитель директора по административно-хозяйственной работе",
     "зам по арх, зам директора по арх, замдиректора по арх"),
    ("Заместитель директора по начальной школе",
     "зам по начальной школе, замдиректора по начальной школе, зам по ноо"),
    ("Заместитель директора по безопасности", "зам по безопасности"),
    ("Заместитель директора по социальной работе",
     "зам по ср, зам директора по ср, зам по социальной работе"),
    ("Заместитель директора по информационно-коммуникационным технологиям",
     "зам по икт, зам по информатизации"),
    ("Заместитель директора по проектной деятельности",
     "зам по проектной деятельности, руководитель проектной деятельности, руководитель цпд"),
    ("Советник директора по воспитанию", "советник по воспитанию, советник директора по воспитанию"),
    ("Главный бухгалтер", "главбух"),
    ("Классный руководитель", "классные руководители, классные руководители классов"),
    ("Руководитель ШМО", "руководители шмо, председатель шмо, руководители методических объединений"),
    ("Педагог-психолог", "психолог"),
    ("Заведующий ИБЦ", "заведующий ибц, библиотекарь"),
]

_DROP_RE = re.compile(r"[.,;:«»\"'()!?]")
_SPACE_RE = re.compile(r"\s+")


def normalize(value: str) -> str:
    """Нормализация для сопоставления: нижний регистр, ё→е, без точек/кавычек/скобок."""
    value = (value or "").lower().replace("ё", "е")
    value = _DROP_RE.sub(" ", value)
    value = value.replace("\xa0", " ")
    return _SPACE_RE.sub(" ", value).strip()


def position_keys(pos: Position) -> list[str]:
    """Ключи должности: нормализованное название + синонимы."""
    keys = [normalize(pos.title)]
    for alias in (pos.aliases or "").split(","):
        alias = normalize(alias)
        if alias and alias not in keys:
            keys.append(alias)
    return [k for k in keys if len(k) >= 4]


def user_resp_keys(user: User) -> set[str]:
    """Ключи ссылок, по которым мероприятия «принадлежат» сотруднику."""
    keys = {str(user.id)}
    for pos in user.positions or []:
        keys.add(f"pos:{pos.id}")
    if user.role in (Role.OWNER, Role.ADMIN):
        keys.add(ADMIN_REF)
    return keys


def refs_match(refs: Iterable[str], keys: set[str]) -> bool:
    return any(str(r) in keys for r in refs or [])


class RespDirectory:
    """Справочники для распознавания ответственных: сотрудники + должности."""

    def __init__(self, users: list[User], positions: list[Position]):
        self.users = users
        self.positions = positions
        self._by_name: dict[str, User] = {}
        for u in users:
            self._by_name[normalize(u.full_name)] = u
            parts = u.full_name.split()
            if parts:
                initials = "".join(f"{p[0]}." for p in parts[1:3] if p)
                short = normalize(f"{parts[0]} {initials}")
                self._by_name.setdefault(short, u)
        self._pos_keys: list[tuple[Position, list[str]]] = [
            (p, position_keys(p)) for p in positions
        ]

    # ------------------------------------------------------------------ match
    def match_chunk(self, chunk: str) -> str | None:
        """Одна запятая-часть текста ответственных → ссылка или None."""
        value = normalize(chunk.strip(" ;,\n"))
        if not value:
            return None
        if value in ADMIN_WORDS:
            return ADMIN_REF
        # 1) должность: точное совпадение ключа
        for pos, keys in self._pos_keys:
            if value in keys:
                return f"pos:{pos.id}"
        # 2) сотрудник: точное совпадение имени/короткого имени
        user = self._by_name.get(value)
        if user is not None:
            return str(user.id)
        # 3) должность: ключ внутри фрагмента («руководители ШМО математиков»)
        for pos, keys in self._pos_keys:
            if any(len(k) >= 6 and k in value for k in keys):
                return f"pos:{pos.id}"
        # 4) сотрудник: имя внутри фрагмента
        for key, u in self._by_name.items():
            if len(key) >= 8 and key in value:
                return str(u.id)
        return None

    def split_matches(self, text: str) -> tuple[list[str], list[str]]:
        """Текст ответственных → (ссылки, нераспознанные фрагменты)."""
        refs: list[str] = []
        unknown: list[str] = []
        for chunk in re.split(r"[,;\n]", text or ""):
            chunk = chunk.strip()
            if not chunk:
                continue
            ref = self.match_chunk(chunk)
            if ref is not None:
                if ref not in refs:
                    refs.append(ref)
            else:
                unknown.append(chunk)
        return refs, unknown


async def load_directory(session: AsyncSession) -> RespDirectory:
    users = (
        await session.execute(
            select(User).where(User.is_deleted.is_(False), User.is_active.is_(True))
        )
    ).scalars().all()
    positions = (await session.execute(select(Position).order_by(Position.sort_order))).scalars().all()
    return RespDirectory(list(users), list(positions))


async def resolve_refs(session: AsyncSession, refs: Iterable[str]) -> list[User]:
    """Раскрытие ссылок в сотрудников: uuid напрямую, pos:→держатели, admin→OWNER/ADMIN."""
    refs = [str(r) for r in refs or []]
    if not refs:
        return []
    user_ids: list[uuid.UUID] = []
    pos_ids: list[uuid.UUID] = []
    need_admin = False
    for ref in refs:
        if ref == ADMIN_REF:
            need_admin = True
        elif ref.startswith("pos:"):
            try:
                pos_ids.append(uuid.UUID(ref[4:]))
            except ValueError:
                continue
        elif ref.startswith("text:"):
            continue
        else:
            try:
                user_ids.append(uuid.UUID(ref))
            except ValueError:
                continue
    cond = [User.is_deleted.is_(False)]
    rows: list[User] = []
    if user_ids:
        rows += (await session.execute(select(User).where(User.id.in_(user_ids), *cond))).scalars().all()
    if pos_ids:
        holders = (
            await session.execute(
                select(User).join(User.positions).where(Position.id.in_(pos_ids), *cond)
            )
        ).scalars().all()
        rows += list(holders)
    if need_admin:
        admins = (
            await session.execute(select(User).where(User.role.in_([Role.OWNER, Role.ADMIN]), *cond))
        ).scalars().all()
        rows += list(admins)
    seen: dict[uuid.UUID, User] = {}
    for u in rows:
        seen.setdefault(u.id, u)
    return list(seen.values())


async def ref_titles(session: AsyncSession, refs: Iterable[str]) -> list[str]:
    """Человекочитаемые названия ссылок (для показа в плане/ВШК)."""
    refs = [str(r) for r in refs or []]
    if not refs:
        return []
    titles: dict[str, str] = {}
    user_ids: list[uuid.UUID] = []
    pos_ids: list[uuid.UUID] = []
    texts: list[str] = []
    for ref in refs:
        if ref == ADMIN_REF:
            titles[ref] = "Администрация"
        elif ref.startswith("pos:"):
            try:
                pos_ids.append(uuid.UUID(ref[4:]))
            except ValueError:
                continue
        elif ref.startswith("text:"):
            texts.append(ref[5:])
        else:
            try:
                user_ids.append(uuid.UUID(ref))
            except ValueError:
                continue
    if user_ids:
        for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all():
            titles[str(u.id)] = u.full_name
    if pos_ids:
        for p in (await session.execute(select(Position).where(Position.id.in_(pos_ids)))).scalars().all():
            titles[f"pos:{p.id}"] = p.title
    out: list[str] = []
    for ref in refs:
        if ref.startswith("text:"):
            out.append(ref[5:])
        else:
            title = titles.get(ref)
            if title:
                out.append(title)
    return out


async def resolve_position_texts(session: AsyncSession, texts: Iterable[str]) -> dict[str, Position]:
    """Тексты должностей (массовый импорт, синхронизация с AD) → записи справочника.

    Точное совпадение названия (без учёта регистра/знаков) — существующая должность;
    неизвестный текст создаёт новую позицию в справочнике (администрация переименует при желании).
    """
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return {}
    existing = (await session.execute(select(Position))).scalars().all()
    by_norm = {normalize(pos.title): pos for pos in existing}
    out: dict[str, Position] = {}
    created = False
    for text in texts:
        key = normalize(text)
        pos = by_norm.get(key)
        if pos is None:
            pos = Position(title=text[:255], aliases="", sort_order=len(by_norm) + 1)
            session.add(pos)
            by_norm[key] = pos
            created = True
        out[text] = pos
    if created:
        await session.flush()
    return out
