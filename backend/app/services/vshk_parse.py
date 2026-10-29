"""Разбор DOCX-документа «План внутришкольного контроля» в мероприятия ВШК.

Ожидаемый макет (как в школьном Word-шаблоне): одна таблица
«Цель контроля | Вид контроля | Ответственный | Срок и форма подведения итогов»,
в которой строки-месяцы (АВГУСТ…ИЮНЬ) и строки-направления контроля оформлены
объединёнными ячейками (все четыре ячейки строки одинаковы).

Ответственные распознаются через справочники сотрудников и должностей
(services.positions): «Зам.директора по УВР» → должность, «Администрация» → вся
администрация, «Кузнецова О.В.» → сотрудник; нераспознанные фрагменты
сохраняются ссылкой «text:…» и попадают в warnings.
"""
from __future__ import annotations

import io
import re
from typing import Any

from app.models.vshk import MONTHS_RU
from app.services.positions import RespDirectory, normalize

_MONTH_NAMES = {normalize(name): month for month, name in MONTHS_RU.items()}
_SKIP_HEAD = {"цель контроля", "вид контроля", "ответственный", "срок и форма подведения итогов"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_vshk_docx(data: bytes, directory: RespDirectory) -> dict[str, Any]:
    """Возвращает {items: [...], warnings: [...]}: месяц, направление, цель, вид, ответственные, итоги."""
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise ValueError("На сервере не установлен python-docx") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ValueError("Файл не является корректным DOCX-документом") from exc

    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    month: int | None = None
    section = ""
    order = 0

    for table in document.tables:
        for row in table.rows:
            cells = [_clean(c.text) for c in row.cells]
            if not any(cells):
                continue
            distinct = {c for c in cells if c}
            first = cells[0]
            low = normalize(first)
            if len(distinct) == 1:
                # строка-месяц или строка-направление (объединённые ячейки)
                if low in _MONTH_NAMES:
                    month = _MONTH_NAMES[low]
                    section = ""
                elif low not in _SKIP_HEAD and len(first) <= 500:
                    section = first
                continue
            if low in _SKIP_HEAD:
                continue
            if month is None:
                # строки до первого месяца (шапка/титул) — пропускаем
                continue
            while len(cells) < 4:
                cells.append("")
            title, control_type, resp_text, summary = cells[0], cells[1], cells[2], cells[3]
            if not title:
                continue
            refs, unknown = directory.split_matches(resp_text)
            refs = [r for r in refs if not r.startswith("text:")]
            refs += [f"text:{u}" for u in unknown]
            if unknown:
                warnings.append(f"{title[:60]}…: не распознаны ответственные «{', '.join(unknown)}»")
            order += 1
            items.append({
                "month": month,
                "section": section[:500],
                "title": title[:1000],
                "control_type": control_type[:255],
                "resp": refs[:40],
                "summary": summary[:500],
                "sort_order": order,
            })

    if not items:
        raise ValueError(
            "В документе не найдена таблица плана ВШК (месяц → направление → строки "
            "«Цель | Вид | Ответственный | Срок и форма»)"
        )
    return {"items": items, "warnings": warnings[:80]}
