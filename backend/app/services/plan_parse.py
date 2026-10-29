"""Разбор DOCX-документа «План работы на неделю» в структуру редактора плана.

Ожидаемый макет (как в школьном Word-шаблоне):
  * шапка: «План работы», период «12.10.2026г. – 17.10.2026г.»;
  * «Дежурный класс: 9Б, кл.рук. Каменских Д.Д.»;
  * произвольные строки шапки (notes): «В столовой дежурят 7-ые классы»;
  * блоки «В течение месяца: …» и «В течение недели: …» с нумерованными пунктами
    (могут жить и в абзацах, и в ячейках разметочной таблицы; хвосты «Отв.: …»
    и «Место проведения: …» вычленяются);
  * таблица расписания: Дата | Мероприятие | Время | Место проведения | Ответственный,
    дата заполнена только в первой строке дня (далее ячейка пуста/объединена).

Ответственные сопоставляются с сотрудниками SchoolHub по короткому имени
(«Кузнецова О.В.») или полному; неузнанные имена не теряются — додаются
в текст пункта/мероприятия хвостом «Отв.: …».
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import date
from typing import Any

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_PERIOD_RE = re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*г?\.?\s*[–—-]\s*(\d{1,2}\.\d{1,2}\.\d{4})")
_ITEM_SPLIT_RE = re.compile(r"\s*\d{1,2}\.\s+")
_TIME_RE = re.compile(
    r"(\d{1,2}[.:]\d{2}|\d{1,2}[.:]\d{2}\s*[-–]\s*\d{1,2}[.:]\d{2}|по\s+отд|по\s+граф|по\s+расп)",
    re.IGNORECASE,
)
_PERSON_KEYWORDS = re.compile(
    r"(администраци|кл\.\s*рук|классн|руководит|учител|психолог|соц\.\s*педагог|библиот|мед\.)",
    re.IGNORECASE,
)
_RESP_RE = re.compile(r"Отв\.?\s*:\s*", re.IGNORECASE)
_PLACE_RE = re.compile(r"Место\s+проведени[яе]\s*:\s*", re.IGNORECASE)


def short_of(full_name: str) -> str:
    parts = full_name.split()
    if not parts:
        return ""
    initials = "".join(f"{p[0]}." for p in parts[1:3] if p)
    return f"{parts[0]} {initials}".strip()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().replace("\xa0", " ")


def _parse_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


class _RespMatcher:
    """Сопоставление имён и должностей из документа со справочниками школы.

    Тонкая обёртка над services.positions.RespDirectory: результат — ссылки
    («<uuid>» | «pos:<uuid>» | «admin»), нераспознанные фрагменты не теряются.
    """

    def __init__(self, directory):
        self._dir = directory

    def match(self, chunk: str) -> str | None:
        return self._dir.match_chunk(chunk)

    def match_user(self, chunk: str):
        """Ссылка только на конкретного сотрудника (для дежурного класса)."""
        ref = self._dir.match_chunk(chunk)
        if ref and not ref.startswith(("pos:", "admin", "text:")):
            return ref
        return None

    def split_matches(self, text: str) -> tuple[list[str], list[str]]:
        return self._dir.split_matches(text)


def _looks_like_person(text: str, matcher: _RespMatcher) -> bool:
    if not text:
        return False
    ids, unknown = matcher.split_matches(text)
    if ids and len(unknown) <= len(ids):
        return True
    return bool(_PERSON_KEYWORDS.search(text)) and not _TIME_RE.search(text)


def _split_resp_place(item: str) -> tuple[str, str | None, str]:
    """Возвращает (текст, место, текст_ответственных)."""
    text, place, resp = item, None, ""
    m = _PLACE_RE.search(text)
    if m:
        tail = text[m.end():]
        r = _RESP_RE.search(tail)
        place = (tail[: r.start()] if r else tail).strip() or None
        text = text[: m.start()] + (tail[r.end():] if r else "")
    m = _RESP_RE.search(text)
    if m:
        resp = text[m.end():].strip()
        text = text[: m.start()]
    return _clean(text), place, resp


def _parse_section_items(raw: str, matcher: _RespMatcher) -> list[dict[str, Any]]:
    # refs — ссылки ответственных: «<uuid>» | «pos:<uuid>» | «admin»
    items: list[dict[str, Any]] = []
    for chunk in _ITEM_SPLIT_RE.split(raw):
        chunk = _clean(chunk)
        if not chunk:
            continue
        text, place, resp_text = _split_resp_place(chunk)
        if not text:
            continue
        refs, unknown = matcher.split_matches(resp_text)
        if unknown:
            text = f"{text} Отв.: {', '.join(unknown)}"
        items.append({"text": text[:1000], "place": (place or None)[:200] if place else None, "resp": refs})
    return items


def parse_plan_docx(data: bytes, directory) -> dict[str, Any]:
    """directory — services.positions.RespDirectory (сотрудники + должности)."""
    try:
        import docx  # python-docx
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph
    except ImportError as exc:  # pragma: no cover
        raise ValueError("На сервере не установлен python-docx") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ValueError("Файл не является корректным DOCX-документом") from exc

    matcher = _RespMatcher(directory)

    def _is_schedule(table) -> bool:
        if len(table.rows) < 2 or len(table.columns) < 4:
            return False
        head = " ".join(_clean(c.text).lower() for c in table.rows[0].cells)
        return "дата" in head and "мероприят" in head

    # --- обход тела документа в порядке следования ---
    blocks: list[str] = []
    schedule_tables: list[Any] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = _clean(DocxParagraph(child, document).text)
            if text:
                blocks.append(text)
        elif tag == "tbl":
            table = DocxTable(child, document)
            if _is_schedule(table):
                schedule_tables.append(table)
                continue
            for row in table.rows:
                for cell in row.cells:
                    for line in cell.text.split("\n"):
                        line = _clean(line)
                        if line:
                            blocks.append(line)

    # --- период недели ---
    week_start: date | None = None
    for p in blocks[:6]:
        m = _PERIOD_RE.search(p)
        if m:
            week_start = _parse_date(m.group(1))
            break
    if week_start is None:
        for p in blocks[:6]:
            week_start = _parse_date(p)
            if week_start:
                break
    if week_start is not None and week_start.weekday() != 0:
        week_start = date.fromordinal(week_start.toordinal() - week_start.weekday())

    # --- шапка: дежурный класс, примечания, секции «месяц/неделя» ---
    warnings: list[str] = []
    duty_klass: str | None = None
    duty_teacher: uuid.UUID | None = None
    notes: list[str] = []
    month_items: list[dict[str, Any]] = []
    week_items: list[dict[str, Any]] = []
    section: str | None = None

    for p in blocks:
        low = p.lower()
        if low.startswith("план работы"):
            continue
        if section is None and _PERIOD_RE.search(p):
            continue
        if low.startswith("дежурный класс"):
            tail = p.split(":", 1)[1] if ":" in p else ""
            klass_part, _, teacher_part = tail.partition(",")
            duty_klass = _clean(klass_part) or None
            if teacher_part:
                teacher_name = _clean(
                    re.sub(r"^.*?(кл\.\s*рук\.?|классный руководитель)\s*", "", teacher_part, flags=re.IGNORECASE)
                )
                ref = matcher.match_user(teacher_name)
                if ref is not None:
                    duty_teacher = uuid.UUID(ref)
                elif teacher_name:
                    warnings.append(f"Дежурный класс: сотрудник «{teacher_name}» не найден в системе")
            continue
        if low.startswith("в течение месяца"):
            section = "month"
            month_items.extend(_parse_section_items(p.split(":", 1)[1] if ":" in p else p, matcher))
            continue
        if low.startswith("в течение недели"):
            section = "week"
            week_items.extend(_parse_section_items(p.split(":", 1)[1] if ":" in p else p, matcher))
            continue
        if section and re.match(r"^\d{1,2}\.\s", p):
            target = month_items if section == "month" else week_items
            target.extend(_parse_section_items(p, matcher))
            continue
        if section is None:
            if _parse_date(p):
                continue
            notes.append(p[:300])

    # --- таблица расписания ---
    days: list[dict[str, Any]] = []
    day_index: dict[date, dict[str, Any]] = {}
    current: date | None = None

    for table in schedule_tables:
        for row in table.rows:
            cells = [_clean(c.text) for c in row.cells]
            while len(cells) < 5:
                cells.append("")
            if not any(cells):
                continue
            head = cells[0]
            if head and not _DATE_RE.search(head) and head.lower().startswith("дата"):
                continue  # строка заголовка
            if _DATE_RE.search(head):
                current = _parse_date(head)
                if current and current not in day_index:
                    day_index[current] = {"date": current.isoformat(), "events": []}
                    days.append(day_index[current])
            if current is None:
                continue
            rest = [c for c in cells[1:5]]
            title = _clean(rest[0])
            if not title:
                non_empty = [c for c in rest if c]
                if not non_empty:
                    continue
                title = non_empty[0]
                rest = non_empty[1:]
            time_v: str | None = None
            place_v: str | None = None
            resp_text = ""
            for cell in rest[1:]:
                if not cell or cell == time_v:
                    continue
                if time_v is None and _TIME_RE.search(cell) and not _looks_like_person(cell, matcher):
                    time_v = cell[:60]
                elif not resp_text and _looks_like_person(cell, matcher):
                    resp_text = cell
                elif place_v is None and not _looks_like_person(cell, matcher):
                    place_v = cell[:200]
                elif not resp_text:
                    resp_text = cell
            refs, unknown = matcher.split_matches(resp_text)
            if unknown:
                title = f"{title} Отв.: {', '.join(unknown)}"[:500]
            day_index[current]["events"].append(
                {"title": title[:500], "time": time_v, "place": place_v, "resp": refs}
            )

    return {
        "week_start": week_start.isoformat() if week_start else None,
        "content": {
            "duty": {"klass": duty_klass, "teacher_id": str(duty_teacher) if duty_teacher else None},
            "notes": notes[:10],
            "month_items": month_items[:30],
            "week_items": week_items[:40],
            "days": days[:7],
        },
        "warnings": warnings[:50],
    }
