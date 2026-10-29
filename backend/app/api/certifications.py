"""Аттестация и КПК: квалификационные категории и курсы повышения квалификации.

Доступ:
  OWNER/ADMIN — весь раздел (создание/редактирование/импорт/экспорт/удаление);
  TEACHER     — только свои записи, на чтение (видит, когда истекает его категория).

Массовый импорт/экспорт — CSV в кодировке UTF-8 с BOM, разделитель «;»
(открывается Excel/LibreOffice напрямую). Формат колонок — как в экспорте.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from app.config import settings
from app.csvsafe import clean_cell, safe_writer
from app.dbutils import ilike
from app.errors import bad_request, forbidden, not_found
from app.models.certification import Certification, CertKind
from app.models.user import User
from app.schemas.common import Page
from app.schemas.user import UserOut
from app.security import CurrentUser, SessionDep
from app.services import audit, documents, lockdown
from app.services import storage as _storage_mod

router = APIRouter(prefix="/certifications", tags=["certifications"])

KIND_RU = {CertKind.CATEGORY: "Категория", CertKind.CPK: "КПК"}
CSV_COLUMNS = [
    "ФИО", "Тип", "Категория", "Название курса", "Часы", "Учебное заведение",
    "Номер документа", "Дата присвоения/окончания", "Действует до", "Примечание",
]


def is_manager(actor: User) -> bool:
    return actor.role.value in ("OWNER", "ADMIN")


async def _require_manager(actor: CurrentUser) -> User:
    if is_manager(actor):
        return actor
    raise forbidden("Добавлять и менять записи может Администрация и Владелец")


ManagerDep = Annotated[User, Depends(_require_manager)]


# ------------------------------------------------------------------ вычисления
def days_left(row: Certification, today: date | None = None) -> int | None:
    if row.expires_at is None:
        return None
    return (row.expires_at - (today or date.today())).days


def state_of(row: Certification, today: date | None = None) -> str:
    """expired — срок истёк; expiring — истекает в пределах горизонта напоминаний;
    valid — действует; indefinite — бессрочно (expires_at не задан)."""
    left = days_left(row, today)
    if left is None:
        return "indefinite"
    if left < 0:
        return "expired"
    if left <= settings.cert_reminder_days:
        return "expiring"
    return "valid"


# --------------------------------------------------------------------- схемы
class CertificationIn(BaseModel):
    user_id: uuid.UUID
    kind: str = Field(default=CertKind.CATEGORY, pattern="^(CATEGORY|CPK)$")
    category: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=500)
    hours: int | None = Field(default=None, ge=0, le=10000)
    provider: str | None = Field(default=None, max_length=300)
    document_no: str | None = Field(default=None, max_length=200)
    awarded_at: date | None = None
    expires_at: date | None = None
    note: str | None = Field(default=None, max_length=1000)


class CertificationUpdateIn(BaseModel):
    user_id: uuid.UUID | None = None
    kind: str | None = Field(default=None, pattern="^(CATEGORY|CPK)$")
    category: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=500)
    hours: int | None = Field(default=None, ge=0, le=10000)
    provider: str | None = Field(default=None, max_length=300)
    document_no: str | None = Field(default=None, max_length=200)
    awarded_at: date | None = None
    expires_at: date | None = None
    note: str | None = Field(default=None, max_length=1000)


class CertificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user: UserOut
    kind: str
    category: str | None = None
    title: str | None = None
    hours: int | None = None
    provider: str | None = None
    document_no: str | None = None
    awarded_at: date | None = None
    expires_at: date | None = None
    file_name: str | None = None
    has_file: bool = False
    note: str | None = None
    state: str = "valid"
    days_left: int | None = None
    created_at: datetime
    updated_at: datetime


def _out(row: Certification) -> CertificationOut:
    out = CertificationOut.model_validate(row)
    out.has_file = bool(row.file)
    out.state = state_of(row)
    out.days_left = days_left(row)
    return out


def _validate_payload(kind: str, category: str | None, title: str | None) -> None:
    if kind == CertKind.CATEGORY and not (category or "").strip():
        raise bad_request("Для квалификационной категории укажите категорию (например, «Высшая»)")
    if kind == CertKind.CPK and not (title or "").strip():
        raise bad_request("Для курса повышения квалификации укажите название курса")


async def _resolve_user(session, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.is_deleted:
        raise bad_request("Сотрудник не найден или уволен")
    return user


def _audit_changes(row: Certification) -> dict:
    return {
        "employee": lockdown.disp(row.user.full_name if row.user else None, row.user_id),
        "kind": KIND_RU.get(row.kind, row.kind),
        "category": row.category or "",
        "course": row.title or "",
        "hours": row.hours or "",
        "document_no": row.document_no or "",
        "awarded_at": str(row.awarded_at or ""),
        "expires_at": str(row.expires_at or ""),
    }


# -------------------------------------------------------------------- список
def _scoped_stmt(actor: User):
    """Базовый запрос в пределах видимости: учитель — только свои записи."""
    stmt = select(Certification)
    if not is_manager(actor):
        stmt = stmt.where(Certification.user_id == actor.id)
    return stmt


@router.get("", summary="Список записей (учитель видит только свои)")
async def list_certifications(
    session: SessionDep,
    actor: CurrentUser,
    kind: Annotated[str | None, Query(pattern="^(CATEGORY|CPK)$")] = None,
    user_id: uuid.UUID | None = None,
    state: Annotated[str | None, Query(pattern="^(valid|expiring|expired|indefinite)$")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str, Query(pattern="^(expires|user|awarded)$")] = "expires",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=200),
) -> dict:
    today = date.today()
    horizon = today + timedelta(days=max(0, settings.cert_reminder_days))
    conds = []
    if not is_manager(actor):
        conds.append(Certification.user_id == actor.id)
    elif user_id:
        conds.append(Certification.user_id == user_id)
    if kind:
        conds.append(Certification.kind == kind)
    if search:
        conds.append(
            or_(
                ilike(Certification.category, search),
                ilike(Certification.title, search),
                ilike(Certification.provider, search),
                ilike(Certification.document_no, search),
                Certification.user_id.in_(select(User.id).where(ilike(User.full_name, search))),
            )
        )
    if state == "expired":
        conds.append(Certification.expires_at.is_not(None))
        conds.append(Certification.expires_at < today)
    elif state == "expiring":
        conds.append(Certification.expires_at.is_not(None))
        conds.append(Certification.expires_at >= today)
        conds.append(Certification.expires_at <= horizon)
    elif state == "valid":
        conds.append(Certification.expires_at.is_not(None))
        conds.append(Certification.expires_at > horizon)
    elif state == "indefinite":
        conds.append(Certification.expires_at.is_(None))

    stmt = select(Certification).where(*conds) if conds else select(Certification)
    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())

    sort_cols = {
        "expires": (Certification.expires_at.is_(None), Certification.expires_at),
        "awarded": (Certification.awarded_at,),
        "user": (Certification.user_id,),
    }
    cols = sort_cols.get(sort, sort_cols["expires"])
    stmt = stmt.order_by(*[c.desc().nulls_last() if order == "desc" else c.asc().nulls_last() for c in cols])
    stmt = stmt.limit(per_page).offset((page - 1) * per_page)
    rows = (await session.execute(stmt)).scalars().all()

    # счётчики по состояниям — для плашек «истекают / просрочены» (в пределах видимости)
    all_rows = (await session.execute(_scoped_stmt(actor))).scalars().all()
    counts = {"all": len(all_rows), "valid": 0, "expiring": 0, "expired": 0, "indefinite": 0}
    for r in all_rows:
        counts[state_of(r, today)] += 1

    payload = Page[CertificationOut](
        items=[_out(r) for r in rows],
        total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )
    return {**payload.model_dump(), "state_counts": counts}


@router.get("/export.csv", summary="Экспорт в CSV (формат Excel: «;», UTF-8 BOM)")
async def export_csv(session: SessionDep, actor: ManagerDep) -> Response:
    rows = (
        await session.execute(
            select(Certification).order_by(Certification.kind, Certification.expires_at.asc().nulls_last())
        )
    ).scalars().all()
    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            lockdown.disp(r.user.full_name if r.user else None, r.user_id),
            KIND_RU.get(r.kind, r.kind),
            r.category or "",
            r.title or "",
            r.hours or "",
            r.provider or "",
            r.document_no or "",
            r.awarded_at.strftime("%d.%m.%Y") if r.awarded_at else "",
            r.expires_at.strftime("%d.%m.%Y") if r.expires_at else "",
            r.note or "",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="certifications_{date.today():%Y-%m-%d}.csv"'},
    )


@router.get("/import/template.csv", summary="Шаблон CSV для массового импорта")
async def import_template(actor: ManagerDep) -> Response:
    buf = io.StringIO()
    writer = safe_writer(buf, delimiter=";")
    writer.writerow(CSV_COLUMNS)
    writer.writerow([
        "Иванова Мария Петровна", "Категория", "Высшая", "", "", "", "приказ № 123-к",
        "15.03.2023", "", "высшая категория устанавливается бессрочно",
    ])
    writer.writerow([
        "Иванова Мария Петровна", "КПК", "", "Цифровая грамотность педагога", "72",
        "ИРО Кировской области", "уд. № 456", "20.10.2025", "", "",
    ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="certifications_template.csv"'},
    )


def _norm_name(value: str) -> str:
    """Нормализация ФИО/текста для сопоставления: регистр, «ё», лишние пробелы."""
    return re.sub(r"\s+", " ", (value or "").replace("ё", "е").replace("Ё", "Е").strip().lower())


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(value)


@router.post("/import", summary="Массовый импорт из CSV (формат как у экспорта)")
async def import_csv(
    session: SessionDep,
    actor: ManagerDep,
    file: Annotated[UploadFile, File(description="CSV: ФИО;Тип;Категория;Название курса;Часы;Учебное заведение;Номер документа;Дата;Действует до;Примечание")],
) -> dict:
    """Строка с тем же сотрудником, типом, названием/категорией и датой — обновляется,
    остальные создаются. Ошибки в отдельных строках не прерывают весь пакет."""
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise bad_request("Файл больше 5 МБ")
    text = data.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise bad_request("Файл пуст")
    first = lines[0]
    delimiter = max((";", "\t", ","), key=lambda d: first.count(d)) if max(first.count(";"), first.count("\t"), first.count(",")) else ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if rows and rows[0][0].strip().lower() in ("фио", "сотрудник", "full_name", "имя"):
        rows = rows[1:]

    # сотрудники один раз: нормализованное ФИО → user
    users = (
        await session.execute(select(User).where(User.is_deleted.is_(False)))
    ).scalars().all()
    by_name: dict[str, User] = {}
    for u in users:
        by_name.setdefault(_norm_name(u.full_name), u)

    created = updated = 0
    failed: list[dict] = []
    for idx, raw in enumerate(rows, start=1):
        cells = [clean_cell(c) for c in (raw + [""] * 10)[:10]]
        fio, kind_s, category, title, hours_s, provider, doc_no, awarded_s, expires_s, note = cells
        if not fio:
            failed.append({"row": idx, "reason": "пустое ФИО"})
            continue
        user = by_name.get(_norm_name(fio))
        if user is None:
            failed.append({"row": idx, "reason": f"сотрудник не найден: {fio!r}"})
            continue
        kind_norm = _norm_name(kind_s)
        if kind_norm in ("категория", "category", "кк", "аттестация"):
            kind = CertKind.CATEGORY
        elif kind_norm in ("кпк", "cpk", "курс", "курсы", "course"):
            kind = CertKind.CPK
        else:
            failed.append({"row": idx, "reason": f"непонятный тип: {kind_s!r} (ожидается «Категория» или «КПК»)"})
            continue
        try:
            awarded_at = _parse_date(awarded_s)
            expires_at = _parse_date(expires_s)
        except ValueError as exc:
            failed.append({"row": idx, "reason": f"некорректная дата: {exc}"})
            continue
        hours = None
        if hours_s:
            try:
                hours = int(hours_s.replace(" ", ""))
                if hours < 0:
                    raise ValueError
            except ValueError:
                failed.append({"row": idx, "reason": f"некорректное количество часов: {hours_s!r}"})
                continue
        if kind == CertKind.CATEGORY and not category:
            failed.append({"row": idx, "reason": "для категории не заполнена колонка «Категория»"})
            continue
        if kind == CertKind.CPK and not title:
            failed.append({"row": idx, "reason": "для КПК не заполнена колонка «Название курса»"})
            continue

        # дедупликация: тот же сотрудник + тип + дата + название/категория.
        # Сравниваем нормализованный текст в Python: SQL lower() в SQLite не
        # работает с кириллицей, а записей на одного сотрудника немного.
        candidates = (
            await session.execute(
                select(Certification).where(
                    Certification.user_id == user.id,
                    Certification.kind == kind,
                    Certification.awarded_at.is_(None) if awarded_at is None else Certification.awarded_at == awarded_at,
                )
            )
        ).scalars().all()
        key_norm = _norm_name(category if kind == CertKind.CATEGORY else title)
        existing = next(
            (
                c for c in candidates
                if _norm_name((c.category if kind == CertKind.CATEGORY else c.title) or "") == key_norm
            ),
            None,
        )
        if existing is not None:
            existing.hours = hours
            existing.provider = provider or None
            existing.document_no = doc_no or None
            existing.expires_at = expires_at
            existing.note = note or None
            updated += 1
        else:
            session.add(Certification(
                user_id=user.id, kind=kind,
                category=category or None, title=title or None,
                hours=hours, provider=provider or None, document_no=doc_no or None,
                awarded_at=awarded_at, expires_at=expires_at, note=note or None,
                created_by_id=actor.id,
            ))
            created += 1
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="CERT_IMPORT", entity_type="certification",
        changes={"created": created, "updated": updated, "failed": len(failed)},
    )
    return {"created": created, "updated": updated, "failed": failed, "total": len(rows)}


# ---------------------------------------------------------------------- CRUD
@router.post("", response_model=CertificationOut, status_code=201, summary="Добавить запись")
async def create_certification(payload: CertificationIn, session: SessionDep, actor: ManagerDep) -> CertificationOut:
    user = await _resolve_user(session, payload.user_id)
    _validate_payload(payload.kind, payload.category, payload.title)
    row = Certification(
        user_id=user.id,
        kind=payload.kind,
        category=(payload.category or "").strip() or None,
        title=(payload.title or "").strip() or None,
        hours=payload.hours,
        provider=(payload.provider or "").strip() or None,
        document_no=(payload.document_no or "").strip() or None,
        awarded_at=payload.awarded_at,
        expires_at=payload.expires_at,
        note=(payload.note or "").strip() or None,
        created_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit.write_audit(
        session, actor=actor, action="CERT_CREATE", entity_type="certification", entity_id=row.id,
        changes=_audit_changes(row),
    )
    return _out(row)


async def _get_row(session, actor: User, cert_id: uuid.UUID) -> Certification:
    row = await session.get(Certification, cert_id)
    if row is None:
        raise not_found("Запись об аттестации")
    if not is_manager(actor) and row.user_id != actor.id:
        raise forbidden("Это запись другого сотрудника")
    return row


@router.get("/{cert_id}", response_model=CertificationOut, summary="Карточка записи")
async def get_certification(cert_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> CertificationOut:
    return _out(await _get_row(session, actor, cert_id))


@router.patch("/{cert_id}", response_model=CertificationOut, summary="Изменить запись")
async def update_certification(
    cert_id: uuid.UUID, payload: CertificationUpdateIn, session: SessionDep, actor: ManagerDep
) -> CertificationOut:
    row = await _get_row(session, actor, cert_id)
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise bad_request("Пустой запрос")
    before = _audit_changes(row)
    if "user_id" in patch and patch["user_id"] is not None:
        await _resolve_user(session, patch["user_id"])
    for field, value in patch.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(row, field, value)
    _validate_payload(row.kind, row.category, row.title)
    await session.flush()
    await session.refresh(row)
    await audit.write_audit(
        session, actor=actor, action="CERT_UPDATE", entity_type="certification", entity_id=row.id,
        changes=audit.diff(before, _audit_changes(row)),
    )
    return _out(row)


@router.delete("/{cert_id}", status_code=204, summary="Удалить запись")
async def delete_certification(cert_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> None:
    row = await _get_row(session, actor, cert_id)
    info = _audit_changes(row)
    if row.file:
        _storage_mod.storage.delete(row.file)
    await session.delete(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="CERT_DELETE", entity_type="certification", entity_id=cert_id, changes=info
    )


# ------------------------------------------------------------------ вложение
@router.post("/{cert_id}/file", response_model=CertificationOut, summary="Прикрепить скан (удостоверение/приказ)")
async def upload_file(
    cert_id: uuid.UUID,
    session: SessionDep,
    actor: ManagerDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="PDF, офисные файлы, изображения — до 25 МБ")],
) -> CertificationOut:
    row = await _get_row(session, actor, cert_id)
    mime = (file.content_type or "").lower()
    if mime not in settings.allowed_task_file_mimes:
        raise bad_request("Недопустимый тип файла: " + (mime or "не определён"))
    stored = await _storage_mod.storage.save(
        file.file, file.filename or "file.bin", mime, allowed_mimes=settings.allowed_task_file_mimes
    )
    if row.file:
        _storage_mod.storage.delete(row.file)   # замена: старый скан удаляем
    row.file = stored.storage_path
    row.file_name = stored.original_name
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="CERT_FILE", entity_type="certification", entity_id=row.id,
        changes={"file": row.file_name, "size": stored.size_bytes},
    )
    await session.refresh(row)
    # прогреваем кэш инлайн-превью фоном: без этого первый просмотр DOCX-скана
    # ждал бы конвертер прямо в запросе (review/REVIEW.md §P1-2)
    documents.schedule_pdf_conversion(background, mime, stored.storage_path)
    return _out(row)


@router.get("/{cert_id}/file", summary="Скачать/посмотреть скан документа")
async def download_file(cert_id: uuid.UUID, session: SessionDep, actor: CurrentUser, inline: bool = Query(default=False)):
    row = await _get_row(session, actor, cert_id)
    if not row.file:
        raise not_found("Файл")
    mime = _guess_mime(row)
    info = await documents.preview_info_async(mime, row.file) if inline else None
    renderable = bool(info and info["preview"] == "inline")
    filename = row.file_name or "document"
    if renderable and info["mime"] == documents.PDF_MIME and not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return FileResponse(
        path=str(_storage_mod.storage.abspath(info["path"] if info else row.file)),
        media_type=info["mime"] if info else mime,
        filename=filename,
        content_disposition_type="inline" if renderable else "attachment",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


def _guess_mime(row: Certification) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(row.file_name or row.file or "")
    return mime or "application/octet-stream"


@router.delete("/{cert_id}/file", response_model=CertificationOut, summary="Удалить скан")
async def delete_file(cert_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> CertificationOut:
    row = await _get_row(session, actor, cert_id)
    if not row.file:
        raise not_found("Файл")
    _storage_mod.storage.delete(row.file)
    row.file = None
    row.file_name = None
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="CERT_FILE_DELETE", entity_type="certification", entity_id=row.id, changes={}
    )
    await session.refresh(row)
    return _out(row)
