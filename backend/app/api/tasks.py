"""API Task Manager (v1.2).

Видимость:
  OWNER/ADMIN — все задачи школы, любые назначения;
  TEACHER     — только свои (где он исполнитель или постановщик), статус/описание/
                комментарии/отметка «выполнено»/вложения.

Исполнителей у задачи может быть НЕСКОЛЬКО; у каждого — своя отметка «выполнено»
(POST/DELETE /tasks/{id}/my-mark). Когда отметки поставили все — задача закрывается.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.enums import PdfStatus, Role
from app.errors import forbidden, not_found
from app.models.task import Task, TaskOccurrence
from app.models.user import User
from app.schemas.common import Page
from app.schemas.task import (
    CommentIn,
    CommentOut,
    StatusChangeIn,
    TaskCreateIn,
    TaskDetailOut,
    TaskOccurrenceOut,
    TaskOut,
    TaskUpdateIn,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import document_pdf, documents
from app.services import storage as _storage_mod
from app.services import tasks as service

router = APIRouter(prefix="/tasks", tags=["tasks"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]


# ---------------------------------------------------------------- serializers
async def _fill_incoming_reg(session, outs) -> None:
    """Подтягивает «№ регистрации» журнала входящих для задач с входящим номером."""
    from app.models.incoming import IncomingLetter

    ids = [o.id for o in outs if o.incoming_number]
    if not ids:
        return
    rows = (
        await session.execute(
            select(IncomingLetter.task_id, IncomingLetter.reg_no, IncomingLetter.reg_year).where(
                IncomingLetter.task_id.in_(ids)
            )
        )
    ).all()
    reg = {tid: f"{no}/{year}" for tid, no, year in rows}
    for o in outs:
        o.incoming_reg = reg.get(o.id)


def _task_out(task: Task, actor: User) -> TaskOut:
    my = task.mark_of(actor.id)
    return TaskOut(
        id=task.id,
        title=task.title,
        description=task.description,
        incoming_number=task.incoming_number,
        status=task.status,
        effective_status=service.effective_status(task),
        priority=task.priority,
        deadline=task.deadline,
        recurrence=task.recurrence or "NONE",
        recurrence_weekdays=task.recurrence_weekdays,
        recurrence_day=task.recurrence_day,
        last_done_at=task.last_done_at,
        done_this_period=service.is_done_this_period(task),
        missed_streak=int(task.missed_streak or 0),
        last_occurrence_start=task.last_occurrence_start,
        last_occurrence_status=task.last_occurrence_status,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
        creator=task.creator,
        assignees=list(task.assignees),
        is_overdue=service.is_overdue(task),
        days_left=service.days_left(task),
        can_edit=service.can_edit(actor, task),
        can_mark=my is not None,
        my_done=bool(my and my.done_at),
        marks_done=task.marks_done,
        files_count=len(task.files),
    )


def _task_detail(task: Task, actor: User) -> TaskDetailOut:
    base = _task_out(task, actor).model_dump()
    return TaskDetailOut(**base, comments=task.comments, history=task.history, files=task.files)


# --------------------------------------------------------------------- list
@router.get("", summary="Список задач (учитель — только свои)")
async def list_tasks(
    session: SessionDep,
    actor: CurrentUser,
    status: Annotated[str | None, Query(pattern="^(NEW|IN_PROGRESS|DONE|OVERDUE|ACTIVE)$")] = None,
    assignee_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    priority: Annotated[str | None, Query(pattern="^(LOW|NORMAL|HIGH|URGENT)$")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    sort: Annotated[str, Query(pattern="^(deadline|created|priority|title|updated|reg)$")] = "deadline",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    scope: Annotated[str, Query(pattern="^(auto|mine|school)$")] = "auto",
    recurring: bool | None = Query(default=None, description="true — только повторяющиеся, false — только обычные"),
) -> dict:
    is_staff = actor.role in (Role.OWNER, Role.ADMIN)
    if scope == "school" and not is_staff:
        raise forbidden("Общешкольный список задач доступен только Владельцу и Администрации")
    mine_only = is_staff and scope == "mine"
    rows, total, counts = await service.list_tasks(
        session,
        actor,
        status=status,
        assignee_id=assignee_id,
        creator_id=creator_id,
        priority=priority,
        search=search,
        due_before=due_before,
        due_after=due_after,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        mine_only=mine_only,
        recurring=recurring,
    )
    outs = [_task_out(t, actor) for t in rows]
    await _fill_incoming_reg(session, outs)
    payload = Page[TaskOut](
        items=outs,
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )
    return {**payload.model_dump(), "status_counts": counts}


@router.get("/status-counts", summary="Счётчики по статусам (для табов/канбана)")
async def status_counts(
    session: SessionDep,
    actor: CurrentUser,
    scope: Annotated[str, Query(pattern="^(auto|mine|school)$")] = "auto",
) -> dict:
    is_staff = actor.role in (Role.OWNER, Role.ADMIN)
    if scope == "school" and not is_staff:
        raise forbidden("Общешкольные счётчики доступны только Владельцу и Администрации")
    return await service.status_counts(
        session, actor, only_user=actor if (is_staff and scope == "mine") else None
    )


@router.get("/overdue-report", summary="Отчёт по просроченным задачам (в разрезе исполнителей)")
async def overdue_report(session: SessionDep, actor: CurrentUser) -> dict:
    if actor.role == Role.TEACHER:
        raise forbidden("Отчёт доступен Администрации и Владельцу")
    return {"items": await service.overdue_report(session, actor)}


@router.post("", response_model=TaskOut, status_code=201, summary="Создать задачу (несколько исполнителей)")
async def create_task(payload: TaskCreateIn, session: SessionDep, actor: CurrentUser) -> TaskOut:
    task = await service.create_task(session, actor, payload)
    await session.refresh(task)          # подтянуть eager-связи до сериализации
    return _task_out(task, actor)


@router.get("/{task_id}", response_model=TaskDetailOut, summary="Карточка задачи (исполнители, отметки, файлы, история)")
async def get_task(task_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> TaskDetailOut:
    task = await service.get_task(session, task_id, actor)
    out = _task_detail(task, actor)
    await _fill_incoming_reg(session, [out])
    return out


@router.get(
    "/{task_id}/occurrences",
    response_model=list[TaskOccurrenceOut],
    summary="История повторов повторяющейся задачи (периоды: выполнен / провален)",
)
async def list_occurrences(
    task_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    limit: int = Query(default=60, ge=1, le=365),
) -> list[TaskOccurrenceOut]:
    task = await service.get_task(session, task_id, actor, with_details=False)
    rows = (
        await session.execute(
            select(TaskOccurrence)
            .where(TaskOccurrence.task_id == task.id)
            .order_by(TaskOccurrence.period_start.desc())
            .limit(limit)
        )
    ).scalars().all()
    by_ids = {str(uid) for r in rows for uid in (r.completed_by_ids or [])}
    names: dict[str, str] = {}
    if by_ids:
        users = (
            await session.execute(select(User).where(User.id.in_([uuid.UUID(x) for x in by_ids])))
        ).scalars().all()
        names = {str(u.id): u.full_name for u in users}
    return [
        TaskOccurrenceOut(
            period_start=r.period_start,
            status=r.status,
            completed_at=r.completed_at,
            completed_by=[names.get(str(uid), "пользователь удалён") for uid in (r.completed_by_ids or [])],
            closed_at=r.closed_at,
        )
        for r in rows
    ]


@router.patch("/{task_id}", response_model=TaskOut, summary="Обновить задачу")
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdateIn, session: SessionDep, actor: CurrentUser
) -> TaskOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    task = await service.update_task(session, actor, task, payload)
    await session.refresh(task)
    return _task_out(task, actor)


@router.post("/{task_id}/status", response_model=TaskOut, summary="Сменить статус (state machine)")
async def change_status(
    task_id: uuid.UUID, payload: StatusChangeIn, session: SessionDep, actor: CurrentUser
) -> TaskOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    task = await service.change_status(session, actor, task, payload.status)
    await session.refresh(task)
    return _task_out(task, actor)


# ------------------------------------------------------- отметки «выполнено»
@router.post("/{task_id}/my-mark", response_model=TaskOut, summary="Отметить «выполнено» (исполнитель)")
async def set_my_mark(task_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> TaskOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    task = await service.set_my_mark(session, actor, task)
    await session.refresh(task)
    return _task_out(task, actor)


@router.delete("/{task_id}/my-mark", response_model=TaskOut, summary="Снять отметку «выполнено»")
async def clear_my_mark(task_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> TaskOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    task = await service.clear_my_mark(session, actor, task)
    await session.refresh(task)
    return _task_out(task, actor)


# ------------------------------------------------------------------ вложения
@router.post("/{task_id}/files", status_code=201, summary="Прикрепить файл к задаче")
async def upload_task_file(
    task_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Офисные файлы, изображения, архивы — до 25 МБ")],
) -> TaskDetailOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    row = await service.add_file(session, actor, task, file)
    await session.commit()
    # v1.12: фоном готовим ПОЛНОЦЕННУЮ PDF-копию документа (а не только кэш
    # .preview.pdf), чтобы вложение открывалось в новой вкладке и попадало
    # в архив «Файлы школы». Конвертация после ответа: LibreOffice медленный,
    # а при --workers 1 он остановил бы весь API (review/REVIEW.md §P1-2).
    document_pdf.ensure_pdf_background(background, row)
    task = await service.get_task(session, task_id, actor)
    return _task_detail(task, actor)


@router.get("/{task_id}/files/{file_id}/download", summary="Скачать вложение задачи")
async def download_task_file(
    task_id: uuid.UUID,
    file_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentUser,
    inline: bool = Query(default=False, description="inline — для просмотра во всплывающем окне"),
):
    task = await service.get_task(session, task_id, actor, with_details=False)
    row = next((f for f in task.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")

    if not inline:
        # скачивание — всегда оригинал в авторском формате
        return FileResponse(
            path=str(_storage_mod.storage.abspath(row.storage_path)),
            media_type=row.mime_type, filename=row.original_name,
            headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
        )

    # inline (v1.12): сначала АВТОМАТИЧЕСКАЯ PDF-копия — она переезжает вместе с
    # документом из служебной записки и открывает в новой вкладке любой формат.
    # Если копии нет (DOCX, загруженный напрямую в задачу), пробуем прежний кэш
    # инлайн-превью documents.preview_info_async — он конвертирует лениво.
    if row.pdf_status == PdfStatus.PENDING:
        await document_pdf.ensure_pdf(row)
        await session.commit()
    info = document_pdf.resolve_view(row)
    if info["preview"] != "inline":
        legacy = await documents.preview_info_async(row.mime_type, row.storage_path)
        if legacy and legacy["preview"] == "inline":
            info = {
                "preview": "inline", "mime": legacy["mime"], "path": legacy["path"],
                "filename": (f"{row.original_name}.pdf"
                             if legacy["mime"] == documents.PDF_MIME
                             and not row.original_name.lower().endswith(".pdf")
                             else row.original_name),
            }
    renderable = info["preview"] == "inline"
    return FileResponse(
        path=str(_storage_mod.storage.abspath(info["path"])),
        media_type=info["mime"],
        filename=info["filename"],
        content_disposition_type="inline" if renderable else "attachment",
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{task_id}/files/{file_id}", response_model=TaskDetailOut, summary="Удалить вложение")
async def delete_task_file(
    task_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep, actor: CurrentUser
) -> TaskDetailOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    await service.delete_file(session, actor, task, file_id)
    task = await service.get_task(session, task_id, actor)
    return _task_detail(task, actor)


# ----------------------------------------------------------------- прочее
@router.post("/{task_id}/comments", response_model=CommentOut, status_code=201, summary="Комментарий")
async def add_comment(
    task_id: uuid.UUID, payload: CommentIn, session: SessionDep, actor: CurrentUser
) -> CommentOut:
    task = await service.get_task(session, task_id, actor, with_details=False)
    comment = await service.add_comment(session, actor, task, payload.body)
    await session.refresh(comment)
    return CommentOut.model_validate(comment)


@router.get("/{task_id}/comments", response_model=list[CommentOut], summary="Комментарии задачи")
async def list_comments(task_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> list[CommentOut]:
    task = await service.get_task(session, task_id, actor)
    return [CommentOut.model_validate(c) for c in task.comments]


@router.delete("/{task_id}", status_code=204, summary="Удалить задачу (OWNER/ADMIN)")
async def delete_task(task_id: uuid.UUID, session: SessionDep, manager: ManagerDep) -> None:
    task = await service.get_task(session, task_id, manager, with_details=False)
    await service.delete_task(session, manager, task)
