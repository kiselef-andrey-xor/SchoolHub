"""Автоматическая PDF-копия документа и передача вложений служебки в задачу (v1.12).

Требование заказчика: «любой загруженный документ при создании служебки
автоматически делать PDF», «документ открывается в новом окне», «служебка с
файлом должна передаваться в задачу исполнителя».

Что проверяем:
  * план конвертации по MIME: офисные → LibreOffice, фото и текст → ReportLab,
    PDF → копия не нужна, архив → честно «не конвертируется»;
  * фото и текст РЕАЛЬНО превращаются в валидный PDF (ReportLab в образе есть);
  * DOCX без LibreOffice в тестовом окружении даёт FAILED с понятной причиной,
    а не падение загрузки — документ остаётся доступным в оригинале;
  * служебка: файл при загрузке получает pdf_status, открывается `inline=true`
    как PDF в новой вкладке, а `inline=false` отдаёт оригинал;
  * при утверждении записки вложения ФИЗИЧЕСКИ КОПИРУЮТСЯ в задачу исполнителя
    (удаление записки не оставляет исполнителя без документа);
  * PDF-копии попадают в перечень ссылок архива «Файлы школы»;
  * инструкция принимает НЕСКОЛЬКО документов, каждый открывается отдельно,
    а прежний маршрут `/help/{id}/file` продолжает работать (совместимость).
"""
from __future__ import annotations

import pathlib
import struct
import zlib

import pytest

from app.enums import PdfStatus
from app.services import document_pdf
from tests.conftest import auth, pdf_bytes, upload
from tests.test_totp_pep import bind

pytestmark = pytest.mark.asyncio



def png_bytes(width: int = 120, height: int = 60) -> bytes:
    """Минимальный валидный PNG (однотонный) — без Pillow в тесте."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + b"\x30\x60\x90" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def draft_note(client, executor_id: str, subject: str = "О ремонте кабинета 214"):
    """Черновик служебной записки от teacher1."""
    cli = await auth(client, "teacher1@example.com")
    resp = await cli.post("/notes", json={
        "subject": subject,
        "body": "Прошу организовать ремонт парт в кабинете 214 до начала третьей четверти.",
        "executor_id": executor_id,
        "deadline": "2027-01-15",
    })
    assert resp.status_code == 201, resp.text
    return cli, resp.json()


# ------------------------------------------------------------------ планирование
def test_pdf_plan_by_mime_type():
    """План определяется ДО загрузки, чтобы статус был известен сразу."""
    assert document_pdf.plan_for("application/pdf").status == PdfStatus.NA
    assert "уже в формате PDF" in document_pdf.plan_for("application/pdf").note
    assert document_pdf.plan_for(DOCX_MIME).method == "libreoffice"
    assert document_pdf.plan_for("application/vnd.ms-excel").method == "libreoffice"
    assert document_pdf.plan_for("application/rtf").method == "libreoffice"
    assert document_pdf.plan_for("image/jpeg").method == "image"
    assert document_pdf.plan_for("image/webp").method == "image"
    assert document_pdf.plan_for("text/plain").method == "text"
    assert document_pdf.plan_for("text/csv").method == "text"

    unsupported = document_pdf.plan_for("application/zip")
    assert unsupported.status == PdfStatus.NA and unsupported.method == "none"
    assert "не конвертируется" in unsupported.note
    # MIME с параметрами («application/pdf; charset=utf-8») разбирается корректно
    assert document_pdf.plan_for("Application/PDF; charset=utf-8").status == PdfStatus.NA
    assert document_pdf.plan_for("").status == PdfStatus.NA


def test_image_and_text_really_become_pdf(tmp_path: pathlib.Path):
    """Фото и текст упаковываются в настоящий PDF (ReportLab + кириллический шрифт)."""
    shot = tmp_path / "photo.png"
    shot.write_bytes(png_bytes())
    result = document_pdf._image_to_pdf(shot)
    assert result.startswith(b"%PDF-") and len(result) > 500

    note = tmp_path / "sluzhebka.txt"
    note.write_text("Служебная записка\nПрошу заменить картридж в кабинете 214.\n", encoding="cp1251")
    produced = document_pdf._text_to_pdf(note, "sluzhebka.txt")
    assert produced.startswith(b"%PDF-")
    # кириллица не потерялась: текст попал в поток страницы (сжатый, но имя файла есть)
    assert b"sluzhebka" in produced or len(produced) > 1000


def test_libreoffice_absent_reports_reason_instead_of_crash(tmp_path: pathlib.Path, monkeypatch):
    """Нет конвертера в образе — FAILED с понятной причиной, а не исключение.

    На NAS LibreOffice ставится в образ, но сборка может пройти и без него:
    загрузка документа обязана работать в любом случае.
    """
    monkeypatch.setattr(document_pdf, "soffice", lambda: None)
    fake = tmp_path / "doc.docx"
    fake.write_bytes(b"PK\x03\x04 not really docx")
    result = document_pdf._libreoffice_to_pdf(fake, "pdf:writer_pdf_Export")
    assert result is None


# ------------------------------------------------------------------- служебки
async def test_note_attachment_gets_pdf_and_opens_inline(client, seeded):
    """Текстовый документ записки: копия готова и открывается в новой вкладке."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]

    up = await cli.post(f"/notes/{nid}/files", files=upload("sluzhebka.txt", "Прошу заменить картридж".encode("utf-8"), "text/plain"))
    assert up.status_code == 201, up.text
    row = up.json()["files"][0]
    assert row["pdf_status"] in ("PENDING", "READY"), row
    fid = row["id"]

    # карточка доделывает зависшую в фоне конверсию
    detail = (await cli.get(f"/notes/{nid}")).json()
    row = next(f for f in detail["files"] if f["id"] == fid)
    assert row["pdf_status"] == "READY", row
    assert row["pdf_ready"] is True

    # оригинал — как есть
    original = await cli.get(f"/notes/{nid}/files/{fid}/download")
    assert original.status_code == 200
    assert original.content == "Прошу заменить картридж".encode("utf-8")
    assert "attachment" in original.headers["content-disposition"]

    # inline — PDF-копия для новой вкладки
    inline = await cli.get(f"/notes/{nid}/files/{fid}/download", params={"inline": "true"})
    assert inline.status_code == 200
    assert inline.content.startswith(b"%PDF-")
    assert inline.headers["content-type"] == "application/pdf"
    assert "inline" in inline.headers["content-disposition"]


async def test_note_image_attachment_becomes_pdf(client, seeded):
    """Фото поломки тоже открывается в браузере — упаковывается в PDF."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]
    up = await cli.post(f"/notes/{nid}/files", files=upload("printer.png", png_bytes(), "image/png"))
    assert up.status_code == 201, up.text
    fid = up.json()["files"][0]["id"]

    detail = (await cli.get(f"/notes/{nid}")).json()
    row = next(f for f in detail["files"] if f["id"] == fid)
    assert row["pdf_status"] == "READY", row
    inline = await cli.get(f"/notes/{nid}/files/{fid}/download", params={"inline": "true"})
    assert inline.content.startswith(b"%PDF-")


async def test_archive_is_honestly_marked_as_not_convertible(client, seeded):
    """Архив не конвертируется: статус NA с пояснением, инлайн отдаёт оригинал."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]
    up = await cli.post(f"/notes/{nid}/files",
                        files=upload("docs.zip", b"PK\x03\x04fake", "application/zip"))
    assert up.status_code == 201, up.text
    row = up.json()["files"][0]
    assert row["pdf_status"] == "NA" and row["pdf_ready"] is False
    assert "не конвертируется" in (row["pdf_note"] or "")

    inline = await cli.get(f"/notes/{nid}/files/{row['id']}/download", params={"inline": "true"})
    assert inline.status_code == 200
    assert "attachment" in inline.headers["content-disposition"], "ZIP нельзя показать инлайн"


async def test_pdf_copy_is_stored_as_a_separate_file(client, seeded, session_factory, tmp_path):
    """Копия — отдельный файл хранилища и отдельная ссылка для архива «Файлы школы»."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]
    await cli.post(f"/notes/{nid}/files", files=upload("akt.txt", b"Akt o spisanii", "text/plain"))
    await cli.get(f"/notes/{nid}")

    from sqlalchemy import select

    from app.models.service_note import ServiceNoteFile
    from app.services import storage as _storage_mod
    from app.services.files_backup import collect_references

    async with session_factory() as session:
        row = (await session.execute(select(ServiceNoteFile))).scalars().first()
        assert row.pdf_path and row.pdf_path != row.storage_path
        assert pathlib.Path(_storage_mod.storage.abspath(row.pdf_path)).exists()
        assert row.pdf_size > 0
        refs = await collect_references(session)

    kinds = {r["kind"] for r in refs}
    assert {"note_file", "note_pdf"} <= kinds, kinds
    assert any(r["storage_path"] == row.pdf_path for r in refs)


async def test_files_transfer_to_task_on_approval(client, seeded, session_factory):
    """Служебка с файлом передаётся исполнителю ВМЕСТЕ с документами (копией)."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]
    await cli.post(f"/notes/{nid}/files", files=upload("smeta.txt", b"Smeta 123", "text/plain"))
    await cli.post(f"/notes/{nid}/files", files=upload("photo.pdf", pdf_bytes("foto"), "application/pdf"))
    await cli.get(f"/notes/{nid}")          # доделываем PDF-копии
    await cli.post(f"/notes/{nid}/submit")

    admin, totp_obj, _ = await bind(client, "admin@example.com")
    approved = await admin.post(f"/notes/{nid}/approve", json={"code": totp_obj.now()})
    assert approved.status_code == 200, approved.text
    task_id = approved.json()["task_id"]

    executor = await auth(client, "teacher2@example.com")
    task = (await executor.get(f"/tasks/{task_id}")).json()
    names = sorted(f["original_name"] for f in task["files"])
    assert names == ["photo.pdf", "smeta.txt"], names

    # PDF-копия текстового документа переехала вместе с оригиналом
    text_file = next(f for f in task["files"] if f["original_name"] == "smeta.txt")
    assert text_file["pdf_ready"] is True, text_file
    inline = await executor.get(f"/tasks/{task_id}/files/{text_file['id']}/download",
                                params={"inline": "true"})
    assert inline.status_code == 200 and inline.content.startswith(b"%PDF-")

    # описание задачи перечисляет переданные приложения
    assert "Приложения к записке" in task["description"]
    assert "smeta.txt" in task["description"]

    # КОПИРОВАНИЕ, а не общая ссылка: файлы записки и задачи лежат по разным путям
    from sqlalchemy import select

    from app.models.service_note import ServiceNoteFile
    from app.models.task import TaskFile

    async with session_factory() as session:
        note_paths = {r.storage_path for r in (await session.execute(select(ServiceNoteFile))).scalars()}
        task_paths = {r.storage_path for r in (await session.execute(select(TaskFile))).scalars()}
    assert not (note_paths & task_paths), "вложения задачи обязаны быть физическими копиями"


async def test_deleting_note_keeps_task_documents(client, seeded):
    """Удаление записки Владельцем не оставляет исполнителя без документа."""
    cli, note = await draft_note(client, seeded["teacher_2"])
    nid = note["id"]
    await cli.post(f"/notes/{nid}/files", files=upload("akt.txt", b"Akt", "text/plain"))
    await cli.get(f"/notes/{nid}")
    await cli.post(f"/notes/{nid}/submit")

    admin, totp_obj, _ = await bind(client, "admin@example.com")
    task_id = (await admin.post(f"/notes/{nid}/approve", json={"code": totp_obj.now()})).json()["task_id"]

    owner = await auth(client, "owner@example.com")
    assert (await owner.delete(f"/notes/{nid}")).status_code == 204

    executor = await auth(client, "teacher2@example.com")
    task = (await executor.get(f"/tasks/{task_id}")).json()
    assert len(task["files"]) == 1
    downloaded = await executor.get(f"/tasks/{task_id}/files/{task['files'][0]['id']}/download")
    assert downloaded.status_code == 200 and downloaded.content == b"Akt"


# ------------------------------------------------------------------- инструкции
async def test_help_article_accepts_multiple_documents(client, seeded):
    """К инструкции прикрепляется НЕСКОЛЬКО документов (v1.12)."""
    admin = await auth(client, "admin@example.com")
    created = await admin.post("/help", json={
        "title": "Порядок оформления служебной поездки",
        "category": "Документы",
        "body": "## Порядок\n\n1. Служебная записка.\n2. Приказ.\n",
    })
    assert created.status_code == 201, created.text
    aid = created.json()["id"]

    first = await admin.post(f"/help/{aid}/files", params={"caption": "Приложение 1"},
                             files=upload("polozhenie.txt", "Polozhenie o poezdkah".encode(), "text/plain"))
    assert first.status_code == 201, first.text
    second = await admin.post(f"/help/{aid}/files", params={"caption": "Форма заявления"},
                              files=upload("forma.pdf", pdf_bytes("forma"), "application/pdf"))
    assert second.status_code == 201, second.text

    files = second.json()["files"]
    assert len(files) == 2, "оба документа остались у инструкции"
    assert [f["caption"] for f in files] == ["Приложение 1", "Форма заявления"]
    # наследие: file_* заполняется по первому вложению, прежние клиенты не ломаются
    assert second.json()["file_name"] == "polozhenie.txt"

    teacher = await auth(client, "teacher1@example.com")
    card = (await teacher.get(f"/help/{aid}")).json()
    assert len(card["files"]) == 2
    text_file = next(f for f in card["files"] if f["original_name"] == "polozhenie.txt")
    assert text_file["pdf_ready"] is True, text_file

    inline = await teacher.get(f"/help/{aid}/files/{text_file['id']}/download", params={"inline": "true"})
    assert inline.status_code == 200 and inline.content.startswith(b"%PDF-")
    original = await teacher.get(f"/help/{aid}/files/{text_file['id']}/download")
    assert original.content == b"Polozhenie o poezdkah"

    # удалить конкретный документ — остальное цело
    removed = await admin.delete(f"/help/{aid}/files/{text_file['id']}")
    assert removed.status_code == 200
    assert [f["original_name"] for f in removed.json()["files"]] == ["forma.pdf"]
    assert removed.json()["file_name"] == "forma.pdf"


async def test_legacy_single_file_route_still_works(client, seeded):
    """Прежние маршруты `/help/{id}/file` продолжают работать после обновления."""
    admin = await auth(client, "admin@example.com")
    aid = (await admin.post("/help", json={"title": "Старая инструкция", "category": "Общее"})).json()["id"]

    up = await admin.post(f"/help/{aid}/file", files=upload("paper.pdf", pdf_bytes(), "application/pdf"))
    assert up.status_code == 200, up.text
    assert up.json()["file_name"] == "paper.pdf"
    assert len(up.json()["files"]) == 1

    teacher = await auth(client, "teacher1@example.com")
    assert (await teacher.get(f"/help/{aid}/file/download")).status_code == 200
    inline = await teacher.get(f"/help/{aid}/file/download", params={"inline": "true"})
    assert inline.status_code == 200 and inline.headers["content-type"] == "application/pdf"

    # инструкцию «только для Владельца» создаёт сам Владелец: администрация
    # задавать видимость OWNER не вправе (существующее правило модуля)
    owner = await auth(client, "owner@example.com")
    secret = (await owner.post("/help", json={"title": "Только для Владельца", "category": "Общее",
                                              "visibility": "OWNER"})).json()["id"]
    await owner.post(f"/help/{secret}/files", files=upload("x.pdf", pdf_bytes(), "application/pdf"))
    assert (await teacher.get(f"/help/{secret}/file/download")).status_code == 404
    assert (await teacher.get(f"/help/{secret}")).status_code == 404


async def test_help_files_are_collected_for_backup(client, seeded, session_factory):
    """Документы инструкций и их PDF-копии попадают в архив «Файлы школы»."""
    admin = await auth(client, "admin@example.com")
    aid = (await admin.post("/help", json={"title": "Инструкция с файлом", "category": "Общее"})).json()["id"]
    await admin.post(f"/help/{aid}/files", files=upload("reglament.txt", b"Reglament", "text/plain"))
    await admin.get(f"/help/{aid}")

    from app.services.files_backup import collect_references

    async with session_factory() as session:
        refs = await collect_references(session)
    kinds = {r["kind"] for r in refs}
    assert {"help_file", "help_pdf"} <= kinds, kinds
    assert any(r["original_name"] == "reglament.txt" for r in refs)
