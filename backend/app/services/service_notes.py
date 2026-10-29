"""Сервис служебных записок (модуль 2 ТЗ).

State machine (app.enums.NOTE_TRANSITIONS) — единственная точка изменения статуса:
    DRAFT → ON_APPROVAL;  ON_APPROVAL → APPROVED | REJECTED;  REJECTED → ON_APPROVAL;
    APPROVED → COMPLETED (автоматически, когда закрыта порождённая задача).

Утверждение — ВСЕГДА подпись кодом TOTP (policy=SignaturePolicy.TOTP, без фолбэка на пароль):
если привязки нет — блокируем с подсказкой (totp.check_totp_code).
При утверждении автоматически создаётся задача исполнителю; ссылки взаимные.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import NOTE_TRANSITIONS, NoteStatus, PdfStatus, Role, SignaturePolicy
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.service_note import ServiceNote, ServiceNoteFile
from app.models.task import Task, TaskStatusHistory
from app.models.user import User
from app.schemas.task import TaskCreateIn
from app.services import audit, notifications, totp
from app.services import tasks as task_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_note(session: AsyncSession, note_id: uuid.UUID, actor: User) -> ServiceNote:
    note = await session.get(ServiceNote, note_id)
    if note is None:
        raise not_found("Служебная записка")
    if not can_see(note, actor):
        raise forbidden("Нет доступа к этой служебной записке")
    return note


def can_see(note: ServiceNote, actor: User) -> bool:
    """Видимость записки (v1.11): целиком журнал видят Владелец и сотрудники
    с персональным грантом `notes_full_access`; остальные — только записки,
    где они автор, исполнитель или согласующий. Записки БЕЗ назначенного
    согласующего видны всей администрации — это входящий пул на утверждение."""
    if actor.role == Role.OWNER or getattr(actor, "notes_full_access", False):
        return True
    if note.author_id == actor.id or note.executor_id == actor.id or note.approver_id == actor.id:
        return True
    return note.approver_id is None and actor.role == Role.ADMIN


def apply_visibility(stmt, actor: User):
    """Ограничить запрос записок кругом видимости актора (см. can_see)."""
    if actor.role == Role.OWNER or getattr(actor, "notes_full_access", False):
        return stmt
    cond = (
        (ServiceNote.author_id == actor.id)
        | (ServiceNote.executor_id == actor.id)
        | (ServiceNote.approver_id == actor.id)
    )
    if actor.role == Role.ADMIN:
        cond = cond | ServiceNote.approver_id.is_(None)
    return stmt.where(cond)


def _ensure_transition(current: str, target: str) -> None:
    allowed = NOTE_TRANSITIONS.get(str(current), set())
    if target not in allowed:
        raise conflict(
            f"Переход «{current} → {target}» запрещён. "
            f"Разрешено: {', '.join(sorted(allowed)) or 'нет переходов'}"
        )


# --------------------------------------------------------------------- CRUD
async def create_note(session: AsyncSession, actor: User, *, subject: str, body: str,
                      executor_id: uuid.UUID | None, deadline: datetime | None,
                      approver_id: uuid.UUID | None = None) -> ServiceNote:
    """Записка пишется как задача: автор, тема, текст, вложения, исполнитель, срок.

    approver_id — кого автор просит утвердить (по умолчанию утверждать может
    любая администрация); согласующим может быть только администрация/Владелец.
    """
    if not subject.strip():
        raise bad_request("Укажите тему записки")
    if len(body.strip()) < 10:
        raise bad_request("Текст записки слишком короткий (минимум 10 символов)")
    executor = None
    if executor_id is not None:
        executor = await session.get(User, executor_id)
        if executor is None or executor.is_deleted or not executor.is_active:
            raise bad_request("Исполнитель не найден или недоступен")
    approver = None
    if approver_id is not None:
        approver = await session.get(User, approver_id)
        if approver is None or approver.is_deleted or not approver.is_active:
            raise bad_request("Согласующий не найден или недоступен")
        if approver.role not in (Role.OWNER, Role.ADMIN):
            raise bad_request("Согласующим может быть только Владелец или администрация")
    dl = _aware(deadline)
    if dl and dl < _now():
        raise bad_request("Срок не может быть в прошлом")

    year = _now().year
    max_no = await session.scalar(
        select(func.max(ServiceNote.number)).where(ServiceNote.year == year)
    )
    note = ServiceNote(
        number=(max_no or 0) + 1,
        year=year,
        author_id=actor.id,
        subject=subject.strip(),
        body=body.strip(),
        executor_id=executor.id if executor else None,
        approver_id=approver.id if approver else None,
        deadline=dl,
        status=NoteStatus.DRAFT,
    )
    session.add(note)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="NOTE_CREATE", entity_type="service_note", entity_id=note.id,
        changes={"number": f"{note.number}/{note.year}", "subject": note.subject,
                 "executor": executor.full_name if executor else None,
                 "approver": approver.full_name if approver else None},
    )
    return note


async def update_note(session: AsyncSession, actor: User, note: ServiceNote, *,
                      subject: str | None = None, body: str | None = None,
                      executor_id: uuid.UUID | None = ..., deadline: datetime | None = ...,
                      approver_id: uuid.UUID | None = ...) -> ServiceNote:
    """Правка допустима только в DRAFT/REJECTED и только автору."""
    if note.author_id != actor.id:
        raise forbidden("Изменять записку может только автор")
    if note.status not in (NoteStatus.DRAFT, NoteStatus.REJECTED):
        raise conflict("Менять можно только черновик или доработанную после отклонения записку")
    if subject is not ... and subject is not None and subject.strip():
        note.subject = subject.strip()
    if body is not ... and body is not None and body.strip():
        note.body = body.strip()
    if approver_id is not ...:
        if approver_id is None:
            note.approver_id = None
        else:
            ap = await session.get(User, approver_id)
            if ap is None or ap.is_deleted or not ap.is_active:
                raise bad_request("Согласующий не найден или недоступен")
            if ap.role not in (Role.OWNER, Role.ADMIN):
                raise bad_request("Согласующим может быть только Владелец или администрация")
            note.approver_id = ap.id
    if executor_id is not ...:
        if executor_id is None:
            note.executor_id = None
        else:
            ex = await session.get(User, executor_id)
            if ex is None or ex.is_deleted or not ex.is_active:
                raise bad_request("Исполнитель не найден или недоступен")
            note.executor_id = ex.id
    if deadline is not ...:
        dl = _aware(deadline)
        if dl and dl < _now():
            raise bad_request("Срок не может быть в прошлом")
        note.deadline = dl
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="NOTE_UPDATE", entity_type="service_note", entity_id=note.id,
        changes={"subject": note.subject},
    )
    return note


async def submit_note(session: AsyncSession, actor: User, note: ServiceNote) -> ServiceNote:
    """Черновик (или доработанная после отклонения) → на согласование."""
    if note.author_id != actor.id:
        raise forbidden("Отправлять записку может только автор")
    _ensure_transition(str(note.status.value), NoteStatus.ON_APPROVAL.value)
    if not note.executor_id:
        raise conflict("Перед отправкой укажите исполнителя")
    note.status = NoteStatus.ON_APPROVAL
    note.reject_comment = None
    await session.flush()
    approvers = (await session.execute(
        select(User).where(User.role.in_([Role.OWNER, Role.ADMIN]), User.is_deleted.is_(False))
    )).scalars().all()
    await notifications.push_many(
        session, user_ids=[u.id for u in approvers], type_="NOTE_ON_APPROVAL",
        title=f"Служебная записка {note.number}/{note.year} на согласовании",
        body=f"{note.subject}. Автор: {actor.full_name}",
        entity_type="service_note", entity_id=note.id,
    )
    await audit.write_audit(
        session, actor=actor, action="NOTE_SUBMIT", entity_type="service_note", entity_id=note.id,
        changes={"number": f"{note.number}/{note.year}"},
    )
    return note


async def approve_note(session: AsyncSession, actor: User, note: ServiceNote,
                       code: str | None, word: str | None = None) -> tuple[ServiceNote, Task]:
    """Утверждение: подпись ВСЕГДА кодом TOTP (без фолбэка на пароль) → создаёт задачу."""
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Утверждать служебные записки может администрация")
    # v1.11: автор выбирает согласующего при создании; утвердить может именно он
    # (Владелец — всегда, как супервизор журнала).
    if note.approver_id is not None and note.approver_id != actor.id and actor.role != Role.OWNER:
        raise forbidden("Эта записка назначена на утверждение другому согласующему")
    _ensure_transition(str(note.status.value), NoteStatus.APPROVED.value)

    # Усиленная подпись: всегда TOTP-код; при CODE_WORD-политике — код+слово.
    method = await totp.enforce_signature(session, actor, SignaturePolicy.TOTP.value, code, word)

    note.status = NoteStatus.APPROVED
    note.approver_id = actor.id
    note.approved_at = _now()
    note.approved_sign_method = method.value if hasattr(method, "value") else str(method)

    # Исполнитель обязателен: submit_note это проверяет, но между отправкой и
    # утверждением записку могли изменить. Без исполнителя задачу создавать некому,
    # а `session.get(User, None)` упал бы в 500 — поэтому говорим понятно (409).
    if not note.executor_id:
        raise conflict("У записки не указан исполнитель: задачу создать некому")
    attachments = "\n".join(f"  • {f.original_name}" for f in note.files)
    desc = (
        f"Из служебной записки №{note.number}/{note.year} "
        f"(автор: {note.author.full_name if note.author else '—'}):\n"
        f"{note.body}\n\n"
        + (f"Приложения к записке (прикреплены к задаче):\n{attachments}\n\n" if attachments else "")
        + f"Ссылка на записку: /notes/{note.id}"
    )
    task = await task_service.create_task(
        session, actor,
        TaskCreateIn(
            title=f"[ЗН {note.number}/{note.year}] {note.subject}"[:300],
            description=desc,
            assignee_ids=[note.executor_id],
            deadline=note.deadline,
        ),
    )
    # взаимная ссылка: записка ↔ задача
    note.task_id = task.id
    # v1.12: служебка передаётся исполнителю ВМЕСТЕ с документами — без них задача
    # «отремонтировать кабинету 214» требовала бы идти к автору за приложением
    files_moved = await copy_files_to_task(session, actor, note, task.id)
    session.add(TaskStatusHistory(
        task_id=task.id, from_status=None, to_status=str(task.status.value),
        actor_id=actor.id,
        note=(f"Создана из служебной записки №{note.number}/{note.year} (id={note.id})"
              + (f"; передано вложений: {files_moved}" if files_moved else "")),
    ))
    await session.flush()

    await notifications.push(
        session, user_id=note.author_id, type_="NOTE_APPROVED",
        title=f"Записка {note.number}/{note.year} утверждена",
        body=f"Утвердил(а): {actor.full_name}. Создана задача исполнителю.",
        entity_type="service_note", entity_id=note.id,
    )
    await audit.write_audit(
        session, actor=actor, action="NOTE_APPROVE", entity_type="service_note", entity_id=note.id,
        changes={"number": f"{note.number}/{note.year}", "sign_method": note.approved_sign_method,
                 "task_id": str(task.id), "files": files_moved},
    )
    return note, task


async def reject_note(session: AsyncSession, actor: User, note: ServiceNote, comment: str) -> ServiceNote:
    """Отклонение — возврат автору с обязательным комментарием."""
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Отклонять служебные записки может администрация")
    _ensure_transition(str(note.status.value), NoteStatus.REJECTED.value)
    if not comment or not comment.strip():
        raise bad_request("При отклонении необходим комментарий автору")
    note.status = NoteStatus.REJECTED
    note.reject_comment = comment.strip()[:1000]
    await session.flush()
    await notifications.push(
        session, user_id=note.author_id, type_="NOTE_REJECTED",
        title=f"Записка {note.number}/{note.year} отклонена",
        body=f"Комментарий: {note.reject_comment}",
        entity_type="service_note", entity_id=note.id,
    )
    await audit.write_audit(
        session, actor=actor, action="NOTE_REJECT", entity_type="service_note", entity_id=note.id,
        changes={"number": f"{note.number}/{note.year}", "comment": note.reject_comment},
    )
    return note


async def delete_note(session: AsyncSession, actor: User, note: ServiceNote) -> None:
    """Удаление записки (v1.11).

    Владелец (и сотрудник с грантом notes_full_access) удаляет ЛЮБУЮ записку —
    в том числе утверждённую: журнал — зона ответственности Владельца.
    Автор — только свой черновик или отклонённую (ещё не подписанную).
    Задача, созданная из утверждённой записки, остаётся (её жизненный цикл независим).
    """
    is_full = actor.role == Role.OWNER or getattr(actor, "notes_full_access", False)
    if not is_full:
        if note.author_id != actor.id:
            raise forbidden("Удалять записку может только автор или Владелец")
        if note.status not in (NoteStatus.DRAFT, NoteStatus.REJECTED):
            raise conflict("Автор может удалить только черновик или отклонённую записку")
    label = f"{note.number}/{note.year}" if note.number else f"черновик «{note.subject}»"
    files = list(note.files)
    await session.delete(note)
    await session.flush()
    from app.services import storage as _storage_mod

    for f in files:
        try:
            _storage_mod.storage.delete_pdf_pair(f.storage_path, f.pdf_path)
        except Exception:  # noqa: BLE001 — файл не критичен, запись уже удалена
            pass
    await audit.write_audit(
        session, actor=actor, action="NOTE_DELETE", entity_type="service_note", entity_id=note.id,
        changes={"number": label, "status": str(note.status.value)},
    )


async def sync_completed_from_task(session: AsyncSession, task: Task) -> ServiceNote | None:
    """Когда порождённая записка-задача закрыта — записка становится COMPLETED."""
    note = (await session.execute(
        select(ServiceNote).where(ServiceNote.task_id == task.id)
    )).scalar_one_or_none()
    if note is None or note.status == NoteStatus.COMPLETED:
        return None
    _ensure_transition(str(note.status.value), NoteStatus.COMPLETED.value)
    note.status = NoteStatus.COMPLETED
    await session.flush()
    await notifications.push(
        session, user_id=note.author_id, type_="NOTE_COMPLETED",
        title=f"Записка {note.number}/{note.year} выполнена",
        body="Порождённая задача закрыта исполнителем.",
        entity_type="service_note", entity_id=note.id,
    )
    await audit.write_audit(
        session, actor=None, action="NOTE_COMPLETE", entity_type="service_note", entity_id=note.id,
        changes={"number": f"{note.number}/{note.year}", "via": "task_done"},
    )
    return note


# ------------------------------------------------------------------- вложения
async def add_file(session: AsyncSession, actor: User, note: ServiceNote, file) -> ServiceNoteFile:
    """Прикрепить документ к записке.

    v1.12: сразу после сохранения определяется план автоматической PDF-копии
    (`document_pdf.plan_for`) — PDF уже готов, фото упаковывается ReportLab'ом,
    архив помечается «не конвертируется». Сама конвертация выполняется ФОНОМ
    после ответа клиенту (`ensure_pdf_background` в API), чтобы загрузка
    документа не ждала LibreOffice.
    """
    from app.config import settings
    from app.services import document_pdf
    from app.services import storage as _storage_mod

    if note.author_id != actor.id:
        raise forbidden("Прикреплять файлы к записке может только автор")
    if note.status not in (NoteStatus.DRAFT, NoteStatus.REJECTED):
        raise conflict("Файлы можно добавлять только к черновику")
    mime = (file.content_type or "").lower().split(";")[0].strip()
    stored = await _storage_mod.storage.save(file.file, file.filename or "file.bin", mime,
                                allowed_mimes=settings.allowed_task_file_mimes)
    plan = document_pdf.plan_for(stored.mime_type)
    row = ServiceNoteFile(
        note_id=note.id, storage_path=stored.storage_path, original_name=stored.original_name,
        mime_type=stored.mime_type, size_bytes=stored.size_bytes, checksum=stored.checksum,
        pdf_status=plan.status, pdf_note=plan.note,
        uploaded_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="NOTE_FILE", entity_type="service_note", entity_id=note.id,
        changes={"file": row.original_name, "size": row.size_bytes, "pdf": plan.status.value},
    )
    return row


async def refresh_pending_pdfs(note: ServiceNote) -> int:
    """Доделывает PDF-копии вложения, зависшие в PENDING (перезапуск контейнера
    между загрузкой и фоновой конверсией). Возвращает число доделанных копий."""
    from app.enums import PdfStatus
    from app.services import document_pdf

    done = 0
    for row in note.files:
        if row.pdf_status == PdfStatus.PENDING:
            await document_pdf.ensure_pdf(row)
            done += 1
    return done


async def copy_files_to_task(session: AsyncSession, actor: User, note: ServiceNote,
                             task_id: uuid.UUID) -> int:
    """Переносит вложения записки в задачу исполнителю (v1.12).

    Файлы КОПИРУЮТСЯ физически (`storage.duplicate`), а не передаются ссылкой:
    у записки и у задачи независимые жизненные циклы, и удаление записки
    (Владелец чистит журнал) не должно оставлять исполнителя без документа.

    Вместе с оригиналом переносится и его PDF-копия — иначе исполнителю
    пришлось бы ждать конвертации заново, а на машине без LibreOffice документ
    вообще не открылся бы в браузере.

    Возвращает число перенесённых вложений.
    """
    from app.models.task import TaskFile
    from app.services import storage as _storage_mod

    moved = 0
    for row in note.files:
        try:
            original = _storage_mod.storage.duplicate(row.storage_path, row.original_name, row.mime_type)
        except Exception:  # noqa: BLE001 — файла могло не стать: задачу всё равно создаём
            continue
        copy = TaskFile(
            task_id=task_id, storage_path=original.storage_path, original_name=row.original_name,
            mime_type=row.mime_type, size_bytes=row.size_bytes, checksum=original.checksum,
            uploaded_by_id=row.uploaded_by_id or actor.id,
        )
        if row.pdf_path:
            try:
                duplicate = _storage_mod.storage.duplicate(row.pdf_path, f"{Path(row.original_name).stem}.pdf",
                                                         "application/pdf")
                copy.pdf_path = duplicate.storage_path
                copy.pdf_size = duplicate.size_bytes
                copy.pdf_status = PdfStatus.READY
            except Exception:  # noqa: BLE001 — копия не критична, оригинал перенесён
                copy.pdf_status = PdfStatus.PENDING
        else:
            copy.pdf_status = row.pdf_status
            copy.pdf_note = row.pdf_note
        session.add(copy)
        moved += 1
    if moved:
        await session.flush()
        await audit.write_audit(
            session, actor=actor, action="NOTE_FILES_TO_TASK", entity_type="service_note",
            entity_id=note.id, changes={"number": f"{note.number}/{note.year}", "files": moved},
        )
    return moved


async def delete_file(session: AsyncSession, actor: User, note: ServiceNote, file_id: uuid.UUID) -> None:
    row = next((f for f in note.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    if note.author_id != actor.id or note.status not in (NoteStatus.DRAFT, NoteStatus.REJECTED):
        raise forbidden("Удалять файлы можно только из своего черновика")
    from app.services import storage as _storage_mod
    # пара «оригинал + PDF-копия»: копию storage.delete() не знает (это отдельный файл)
    _storage_mod.storage.delete_pdf_pair(row.storage_path, row.pdf_path)
    note.files.remove(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="NOTE_FILE_DELETE", entity_type="service_note", entity_id=note.id,
        changes={"file": row.original_name},
    )


# -------------------------------------------------------------------- списки
async def list_notes(session: AsyncSession, actor: User, *, status: str | None = None,
                     author_id: uuid.UUID | None = None, executor_id: uuid.UUID | None = None,
                     q: str | None = None, year: int | None = None) -> list[ServiceNote]:
    stmt = select(ServiceNote)
    stmt = apply_visibility(stmt, actor)
    if status:
        stmt = stmt.where(ServiceNote.status == status)
    if author_id:
        stmt = stmt.where(ServiceNote.author_id == author_id)
    if executor_id:
        stmt = stmt.where(ServiceNote.executor_id == executor_id)
    if year:
        stmt = stmt.where(ServiceNote.year == year)
    if q:
        from app.dbutils import ilike
        stmt = stmt.where(ilike(ServiceNote.subject, q))
    stmt = stmt.order_by(ServiceNote.year.desc(), ServiceNote.number.desc())
    return list((await session.execute(stmt)).scalars().all())
