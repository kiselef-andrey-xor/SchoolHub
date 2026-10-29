"""API справки и инструкций (модуль 4 ТЗ): категории, поиск, CRUD, файлы, лист ознакомления.

Чтение — всем авторизованным В ПРЕДЕЛАХ ВИДИМОСТИ статьи; создание/правка
UPLOAD-инструкций — администрация, системных карточек — только Владелец
(правила в app/services/help.py::can_edit).

Видимость (``visibility``) ограничивает круг читателей статьи:
``ALL`` — всем сотрудникам, ``ADMIN`` — только администрации, ``OWNER`` — только
Владельцу. Статья недоступной видимости для роли не существует: её нет в списке,
в категориях и в глобальном поиске, а прямой запрос по id возвращает 404 (а не 403 —
сам факт наличия служебной инструкции раскрывать не нужно).

Флаг mandatory_read создаёт лист ознакомления по механизму приказов; круг
ознакомления равен кругу читателей статьи. Ознакомление с инструкцией, требующей
код, — ПЭП (TOTP), без фолбэка на пароль.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.enums import HelpVisibility, PdfStatus, Role
from app.errors import not_found
from app.models.help import HelpArticle
from app.models.user import User
from app.schemas.journal import (
    HelpAckOut,
    HelpArticleIn,
    HelpArticleOut,
    HelpArticleUpdateIn,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import document_pdf
from app.services import help as service
from app.services import storage as _storage_mod  # модуль, а не экземпляр: фикстуры подменяют storage в тестах

router = APIRouter(prefix="/help", tags=["help"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]


async def _my_ack_map(session, actor_id: uuid.UUID, arts: list[HelpArticle]) -> dict:
    """article_id -> моя отметка об ознакомлении (для my_ack_at в ответах)."""
    from sqlalchemy import select

    from app.models.help import HelpAck

    ids = [a.id for a in arts if a.mandatory_read]
    if not ids:
        return {}
    rows = (await session.execute(
        select(HelpAck).where(HelpAck.article_id.in_(ids), HelpAck.user_id == actor_id,
                              HelpAck.acked_at.is_not(None))
    )).scalars().all()
    return {r.article_id: r for r in rows}


@router.get("/categories", response_model=list[str], summary="Категории доступных статей")
async def list_categories(session: SessionDep, actor: CurrentUser) -> list[str]:
    return await service.categories(session, actor)


@router.get("", response_model=list[HelpArticleOut], summary="Список статей (q — поиск по названию/тексту)")
async def list_articles(
    session: SessionDep,
    actor: CurrentUser,
    q: str | None = Query(default=None, max_length=120),
    category: str | None = Query(default=None, max_length=120),
    kind: str | None = Query(default=None, pattern="^(SYSTEM|UPLOAD)$"),
    visibility: HelpVisibility | None = Query(
        default=None, description="Фильтр по видимости (только из доступных роли значений)"),
):
    arts = await service.list_articles(session, actor, q=q, category=category, kind=kind,
                                       visibility=visibility)
    outs = [HelpArticleOut.model_validate(a) for a in arts]
    mine = await _my_ack_map(session, actor.id, arts)
    for art, out in zip(arts, outs):
        r = mine.get(art.id)
        if r is not None:
            out.my_ack_at = r.acked_at
    return outs


@router.get("/mine/pending", summary="Мои неотознанные обязательные инструкции (для бейджа)")
async def my_pending(session: SessionDep, actor: CurrentUser) -> dict:
    return {"count": await service.my_pending_count(session, actor)}


@router.post("", response_model=HelpArticleOut, status_code=201,
             summary="Создать инструкцию (администрация); mandatory_read открывает лист ознакомления")
async def create_article(payload: HelpArticleIn, session: SessionDep, actor: ManagerDep) -> HelpArticleOut:
    art = await service.create_article(
        session, actor, title=payload.title, category=payload.category,
        body=payload.body, mandatory_read=payload.mandatory_read,
        visibility=payload.visibility,
    )
    await session.refresh(art)
    return HelpArticleOut.model_validate(art)


@router.get("/{article_id}", response_model=HelpArticleOut, summary="Статья; администрации — лист ознакомления")
async def get_article(article_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> HelpArticleOut:
    art = await service.get_article(session, article_id, actor)
    # PDF-копии готовятся фоном: доделываем то, что зависло в PENDING
    if await service.refresh_pending_pdfs(art):
        await session.commit()
    out = HelpArticleOut.model_validate(art)
    if art.mandatory_read:
        from sqlalchemy import select

        from app.models.help import HelpAck

        mine = (await session.execute(
            select(HelpAck).where(HelpAck.article_id == art.id, HelpAck.user_id == actor.id)
        )).scalar_one_or_none()
        if mine is not None and mine.acked_at is not None:
            out.my_ack_at = mine.acked_at
        if service.can_edit(art, actor):
            sheet = await service.ack_sheet(session, actor, art)
            out.acks = [HelpAckOut.model_validate(r) for r in sheet]
    return out


@router.patch("/{article_id}", response_model=HelpArticleOut, summary="Изменить статью")
async def update_article(article_id: uuid.UUID, payload: HelpArticleUpdateIn,
                         session: SessionDep, actor: CurrentUser) -> HelpArticleOut:
    art = await service.get_article(session, article_id, actor)
    art = await service.update_article(
        session, actor, art, title=payload.title, category=payload.category,
        body=payload.body, mandatory_read=payload.mandatory_read,
        visibility=payload.visibility,
    )
    await session.refresh(art)
    return HelpArticleOut.model_validate(art)


@router.delete("/{article_id}", status_code=204, summary="Удалить инструкцию (системные — нельзя)")
async def delete_article(article_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    art = await service.get_article(session, article_id, actor)
    await service.delete_article(session, actor, art)


# ------------------------------------------------------------------- вложения
def _article_out(art: HelpArticle) -> HelpArticleOut:
    """Карточка статьи. Поля-наследие `file_*` заполняет валидатор схемы по
    первому вложению, поэтому здесь достаточно model_validate."""
    return HelpArticleOut.model_validate(art)


@router.post("/{article_id}/files", response_model=HelpArticleOut, status_code=201,
             summary="Прикрепить документ к инструкции (вложений может быть несколько)")
async def upload_files(article_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                       background: BackgroundTasks,
                       file: Annotated[UploadFile, File(description="PDF/DOCX/изображение до 25 МБ")],
                       caption: str | None = Query(default=None, max_length=300,
                                                   description="Подпись: «Приложение 1», «Форма заявления»"),
                       ) -> HelpArticleOut:
    """v1.12: к инструкции прикрепляется НЕСКОЛЬКО документов.

    PDF-копия ставится в фон СРАЗУ после ответа (`ensure_pdf_background`):
    конвертация LibreOffice занимает десятки секунд, а при `--workers 1`
    синхронная конвертация остановила бы весь API (review/REVIEW.md §P1-2).
    """
    art = await service.get_article(session, article_id, actor)
    row = await service.add_file(session, actor, art, file, caption=caption)
    await session.commit()
    document_pdf.ensure_pdf_background(background, row)
    await session.refresh(art)
    return _article_out(art)


@router.get("/{article_id}/files/{file_id}/download",
            summary="Открыть (inline=true) или скачать документ инструкции")
async def download_article_file(article_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep,
                                actor: CurrentUser,
                                inline: bool = Query(default=False,
                                                     description="inline — PDF-копия для новой вкладки")):
    """Отдача конкретного вложения.

    `inline=true` — сценарий v1.12 «документ открывается кликом в новой вкладке»:
    отдаётся автоматическая PDF-копия, поэтому DOCX и фотографии просматриваются
    в браузере без Office на рабочем месте. Если копия ещё не готова — доделываем
    её здесь (фон мог не отработать из-за перезапуска контейнера).

    `inline=false` — всегда оригинал в авторском формате.
    """
    art = await service.get_article(session, article_id, actor)
    row = next((f for f in art.files if f.id == file_id), None)
    if row is None:
        raise not_found("Документ инструкции")
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


@router.delete("/{article_id}/files/{file_id}", response_model=HelpArticleOut,
               summary="Удалить документ инструкции")
async def delete_article_file(article_id: uuid.UUID, file_id: uuid.UUID,
                              session: SessionDep, actor: CurrentUser) -> HelpArticleOut:
    art = await service.get_article(session, article_id, actor)
    await service.delete_file(session, actor, art, file_id)
    await session.commit()
    await session.refresh(art)
    return _article_out(art)


# --- маршруты-наследие (единственный файл статьи). Сохранены, чтобы ссылки
# --- прежних версий интерфейса и закладки не «сломались» после обновления:
# --- они работают с ПЕРВЫМ вложением. Новый код использует /files выше.
@router.post("/{article_id}/file", response_model=HelpArticleOut,
             summary="Загрузить файл инструкции (устаревший маршрут — используйте POST /files)",
             deprecated=True)
async def upload_file(article_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                      background: BackgroundTasks,
                      file: Annotated[UploadFile, File(description="Файл до 25 МБ")]) -> HelpArticleOut:
    art = await service.get_article(session, article_id, actor)
    row = await service.add_file(session, actor, art, file)
    await session.commit()
    document_pdf.ensure_pdf_background(background, row)
    await session.refresh(art)
    return _article_out(art)


@router.get("/{article_id}/file/download",
            summary="Скачать первый документ инструкции (устаревший маршрут)",
            deprecated=True)
async def download_file(article_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                        inline: bool = Query(default=False,
                                             description="inline — для просмотра во всплывающем окне")):
    art = await service.get_article(session, article_id, actor)
    if not art.files:
        raise not_found("Файл инструкции")
    first = art.files[0]
    if inline and first.pdf_status == PdfStatus.PENDING:
        await document_pdf.ensure_pdf(first)
        await session.commit()
    info = document_pdf.resolve_view(first) if inline else {
        "preview": "download", "mime": first.mime_type or "application/octet-stream",
        "path": first.storage_path, "filename": first.original_name,
    }
    return FileResponse(
        path=str(_storage_mod.storage.abspath(info["path"])),
        media_type=info["mime"],
        filename=info["filename"],
        content_disposition_type="inline" if info["preview"] == "inline" else "attachment",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


class AckIn(BaseModel):
    code: str | None = None
    word: str | None = None


@router.post("/{article_id}/ack", response_model=HelpArticleOut,
             summary="Отметка «Ознакомлен»: клик или ПЭП (код TOTP), если инструкция требует код")
async def ack(article_id: uuid.UUID, payload: AckIn, request: Request, session: SessionDep,
              actor: CurrentUser) -> HelpArticleOut:
    art = await service.get_article(session, article_id, actor)
    # требование кода включается системным флагом help_require_code (по умолчанию — достаточно клика);
    # если включён и привязки TOTP нет — service/totp.enforce_signature вернёт 409 с подсказкой
    from sqlalchemy import select

    from app.models.system_flag import SystemFlag

    val = (await session.execute(
        select(SystemFlag.value).where(SystemFlag.key == "help_require_code")
    )).scalar_one_or_none()
    require_code = val in ("1", "true", "on")
    row = await service.ack_article(
        session, actor, art, code=payload.code, word=payload.word,
        ip=request.client.host if request.client else None,
        ua=request.headers.get("user-agent"), require_code=require_code,
    )
    await session.refresh(art)
    out = HelpArticleOut.model_validate(art)
    out.my_ack_at = row.acked_at
    return out
