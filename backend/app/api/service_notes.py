"""API служебных записок (модуль 2 ТЗ).

Права: автор пишет/отправляет свои; администрация — согласует и видит все;
исполнитель видит записку и порождённую задачу. Утверждение — только с ПЭП-кодом.
Журнал: фильтры (?status=&author_id=&executor_id=&year=&q=), экспорт CSV, печатная форма с визой.
"""
from __future__ import annotations

import io
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app.csvsafe import safe_writer
from app.enums import AckSignMethod, PdfStatus, Role
from app.errors import not_found
from app.models.service_note import ServiceNote
from app.models.user import User
from app.schemas.journal import (
    NoteApproveIn,
    NoteDetailOut,
    NoteIn,
    NoteOut,
    NoteRejectIn,
    NoteUpdateIn,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import document_pdf, lockdown, note_print
from app.services import service_notes as service
from app.services import storage as _storage_mod

router = APIRouter(prefix="/notes", tags=["service-notes"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]


def _label(n: ServiceNote) -> str:
    return f"{n.number}/{n.year}" if n.number else "без номера"


_STATUS_RU = {
    "DRAFT": "Черновик",
    "ON_APPROVAL": "На согласовании",
    "APPROVED": "Утверждена",
    "REJECTED": "Отклонена",
    "COMPLETED": "Выполнена",
}
_SIGN_RU = {
    None: "—",
    AckSignMethod.NONE.value: "без подписи (аудит)",
    AckSignMethod.CLICK.value: "клик «Ознакомлен»",
    AckSignMethod.TOTP.value: "код из аутентификатора",
    AckSignMethod.CODE_WORD.value: "код + контрольное слово",
}


@router.post("", response_model=NoteDetailOut, status_code=201, summary="Создать служебную записку (черновик)")
async def create_note(payload: NoteIn, session: SessionDep, actor: CurrentUser) -> NoteDetailOut:
    note = await service.create_note(
        session, actor, subject=payload.subject, body=payload.body,
        executor_id=payload.executor_id, deadline=payload.deadline,
        approver_id=payload.approver_id,
    )
    await session.commit()
    return await _detail(session, note.id, actor)


@router.get("", response_model=list[NoteOut], summary="Журнал служебных записок (фильтры)")
async def list_notes(
    session: SessionDep,
    actor: CurrentUser,
    status: str | None = Query(default=None, max_length=20),
    author_id: uuid.UUID | None = None,
    executor_id: uuid.UUID | None = None,
    year: int | None = Query(default=None, ge=2000, le=2200),
    q: str | None = Query(default=None, max_length=120),
) -> list[NoteOut]:
    rows = await service.list_notes(
        session, actor, status=status, author_id=author_id, executor_id=executor_id, year=year, q=q
    )
    return [NoteOut.model_validate(r) for r in rows]


@router.get("/export.csv", summary="Экспорт журнала записок в CSV")
async def export_notes(
    session: SessionDep,
    actor: CurrentUser,
    status: str | None = None,
    year: int | None = None,
) -> Response:
    rows = await service.list_notes(session, actor, status=status, year=year)
    buf = io.StringIO()
    w = safe_writer(buf, delimiter=";")
    w.writerow(["№", "Год", "Тема", "Автор", "Исполнитель", "Срок", "Статус",
                "Согласующий", "Дата утверждения", "Способ подписи", "Задача"])
    for n in rows:
        # ФИО — через lockdown.disp(): CSV строится мимо Pydantic-схем, поэтому
        # сериализаторы режим «Локдаун» здесь не применят (review/REVIEW.md §P1-1)
        w.writerow([
            n.number or "", n.year or "", n.subject,
            lockdown.disp(n.author.full_name if n.author else None, n.author_id),
            lockdown.disp(n.executor.full_name if n.executor else None, n.executor_id),
            n.deadline.strftime("%d.%m.%Y %H:%M") if n.deadline else "",
            _STATUS_RU.get(str(n.status.value), str(n.status.value)),
            lockdown.disp(n.approver.full_name if n.approver else None, n.approver_id),
            n.approved_at.strftime("%d.%m.%Y %H:%M") if n.approved_at else "",
            _SIGN_RU.get(n.approved_sign_method, n.approved_sign_method or "—"),
            str(n.task_id) if n.task_id else "",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="service_notes.csv"'},
    )


@router.patch("/{note_id}", response_model=NoteDetailOut, summary="Править черновик (только автор)")
async def update_note(note_id: uuid.UUID, payload: NoteUpdateIn, session: SessionDep, actor: CurrentUser):
    note = await service.get_note(session, note_id, actor)
    await service.update_note(
        session, actor, note,
        subject=payload.subject if "subject" in payload.model_fields_set else ...,
        body=payload.body if "body" in payload.model_fields_set else ...,
        deadline=payload.deadline if "deadline" in payload.model_fields_set else ...,
        executor_id=payload.executor_id if "executor_id" in payload.model_fields_set else ...,
        approver_id=payload.approver_id if "approver_id" in payload.model_fields_set else ...,
    )
    await session.commit()
    return await _detail(session, note.id, actor)


@router.post("/{note_id}/submit", response_model=NoteDetailOut, summary="Отправить на согласование")
async def submit_note(note_id: uuid.UUID, session: SessionDep, actor: CurrentUser):
    note = await service.get_note(session, note_id, actor)
    await service.submit_note(session, actor, note)
    await session.commit()
    return await _detail(session, note.id, actor)


@router.post("/{note_id}/approve", response_model=NoteDetailOut,
             summary="Утвердить (подпись кодом TOTP, всегда) → создаёт задачу исполнителю")
async def approve_note(note_id: uuid.UUID, payload: NoteApproveIn, session: SessionDep, actor: ManagerDep):
    note = await service.get_note(session, note_id, actor)
    await service.approve_note(session, actor, note, code=payload.code, word=payload.word)
    await session.commit()
    return await _detail(session, note.id, actor)


@router.post("/{note_id}/reject", response_model=NoteDetailOut, summary="Отклонить → возврат автору с комментарием")
async def reject_note(note_id: uuid.UUID, payload: NoteRejectIn, session: SessionDep, actor: ManagerDep):
    note = await service.get_note(session, note_id, actor)
    await service.reject_note(session, actor, note, payload.comment)
    await session.commit()
    return await _detail(session, note.id, actor)


# ------------------------------------------------------------------- вложения
@router.post("/{note_id}/files", response_model=NoteDetailOut, status_code=201,
             summary="Прикрепить документ (PDF-копия готовится автоматически)")
async def upload_note_file(
    note_id: uuid.UUID, session: SessionDep, actor: CurrentUser, background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Офисные документы, изображения, архивы — до 25 МБ")],
):
    """Загрузка документа к записке.

    v1.12: PDF-копия ставится в фон СРАЗУ после ответа (`ensure_pdf_background`).
    Делать её синхронно нельзя: конвертация LibreOffice на DS223 занимает десятки
    секунд, а при `--workers 1` это остановило бы весь API (review/REVIEW.md §P1-2).
    """
    note = await service.get_note(session, note_id, actor)
    row = await service.add_file(session, actor, note, file)
    await session.commit()
    document_pdf.ensure_pdf_background(background, row)
    return await _detail(session, note.id, actor)


@router.get("/{note_id}/files/{file_id}/download",
            summary="Открыть (inline=true) или скачать вложение записки")
async def download_note_file(note_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                             inline: bool = Query(default=False,
                                                  description="inline — PDF-копия для открытия в новой вкладке")):
    """Отдача документа.

    `inline=true` — то, ради чего в v1.12 появилась автоматическая PDF-копия:
    браузер рендерит PDF нативно, поэтому документ открывается кликом в новой
    вкладке независимо от исходного формата (DOCX, фото, текст). Если копия ещё
    не готова (фон не отработал, контейнер перезапустили) — доделываем её здесь.

    `inline=false` — всегда оригинал в авторском формате.
    """
    note = await service.get_note(session, note_id, actor)
    row = next((f for f in note.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    if inline and row.pdf_status == PdfStatus.PENDING:
        await document_pdf.ensure_pdf(row)
        await session.commit()
    info = document_pdf.resolve_view(row) if inline else {
        "preview": "download", "mime": row.mime_type or "application/octet-stream",
        "path": row.storage_path, "filename": row.original_name,
    }
    return FileResponse(
        path=str(_storage_mod.storage.abspath(info["path"])),
        media_type=info["mime"],
        filename=info["filename"],
        content_disposition_type="inline" if info["preview"] == "inline" else "attachment",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{note_id}/files/{file_id}", response_model=NoteDetailOut, summary="Удалить вложение")
async def delete_note_file(note_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep, actor: CurrentUser):
    note = await service.get_note(session, note_id, actor)
    await service.delete_file(session, actor, note, file_id)
    await session.commit()
    return await _detail(session, note.id, actor)


@router.get("/{note_id}", response_model=NoteDetailOut, summary="Карточка служебной записки")
async def get_note(note_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> NoteDetailOut:
    return await _detail(session, note_id, actor)


@router.delete("/{note_id}", status_code=204,
               summary="Удалить записку (Владелец — любую, автор — черновик/отклонённую)")
async def delete_note(note_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    note = await service.get_note(session, note_id, actor)
    await service.delete_note(session, actor, note)
    await session.commit()


# ------------------------------------------------------------- печатная форма
@router.get("/{note_id}/print", response_class=HTMLResponse,
            summary="Печатная форма служебной записки (бланк, виза, отметка об исполнении)")
async def print_note(
    note_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
    auto_print: bool = Query(default=False, description="true — сразу открыть диалог печати"),
) -> HTMLResponse:
    """Печатная форма документа.

    v1.12: макет переделан полностью (app/services/note_print.py) — бланк с шапкой
    школы и адресатом, заголовочная часть с номером и датой, приложения, реквизиты,
    подпись автора, виза согласования со способом подписи и отметка об исполнении.

    Все пользовательские значения экранируются через htmlsafe.esc: тему, текст и
    имена вложений пишет любой сотрудник, а форму открывает согласующий — без
    экранирования это stored XSS в сессии администрации (review/REVIEW.md §P0-2).
    ФИО дополнительно проходят lockdown.disp().
    """
    note = await service.get_note(session, note_id, actor)
    document = await note_print.render_async(session, note, printed_by=actor, auto_print=auto_print)
    return HTMLResponse(
        document,
        headers={
            # CSP дублируется заголовком: <meta> защищает при открытии напрямую,
            # заголовок — при просмотре через прокси, который meta может не учесть
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "script-src 'unsafe-inline'; img-src data:; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def _detail(session: SessionDep, note_id: uuid.UUID | str, actor: User) -> NoteDetailOut:
    if isinstance(note_id, str):
        # путь мог совпасть со статическим маршрутом (например /notes/export.csv)
        raise not_found("Служебная записка")
    note = await service.get_note(session, note_id, actor)
    await session.refresh(note)
    # PDF-копии готовятся фоном: если контейнер перезапустили между загрузкой и
    # конверсией, доделываем здесь — иначе документ навсегда остался бы «готовится»
    if await service.refresh_pending_pdfs(note):
        await session.commit()
    return NoteDetailOut.model_validate(note)
