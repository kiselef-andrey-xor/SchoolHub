"""Хранилище файлов: проверка сигнатур содержимого и защита от path traversal."""
from __future__ import annotations

import io

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio


async def _make_task_with_file_access(client, seeded):
    a = await auth(client, "admin@example.com")
    task = (
        await a.post(
            "/tasks",
            json={"title": "Задача для файловых тестов", "assignee_ids": [seeded["teacher1@example.com"]],
                  "priority": "NORMAL"},
        )
    ).json()
    return a, task


# ------------------------------------------------------------------ сигнатуры
async def test_fake_pdf_rejected(client, seeded):
    """content_type подделывается заголовком — содержимое обязано проверяться по сигнатуре."""
    a, task = await _make_task_with_file_access(client, seeded)
    resp = await a.post(
        f"/tasks/{task['id']}/files",
        files=upload("document.pdf", b"<html>not a pdf at all</html>", "application/pdf"),
    )
    assert resp.status_code == 400
    assert "не соответствует" in resp.json()["detail"].lower()


async def test_fake_docx_rejected(client, seeded):
    a, task = await _make_task_with_file_access(client, seeded)
    # валидный ZIP, но не OOXML-word (нет word/)
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "world")
    resp = await a.post(
        f"/tasks/{task['id']}/files",
        files=upload("doc.docx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert resp.status_code == 400


async def test_real_docx_accepted(client, seeded):
    a, task = await _make_task_with_file_access(client, seeded)
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
    resp = await a.post(
        f"/tasks/{task['id']}/files",
        files=upload("doc.docx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert resp.status_code == 201, resp.text


async def test_real_pdf_and_image_accepted(client, seeded):
    a, task = await _make_task_with_file_access(client, seeded)
    resp = await a.post(
        f"/tasks/{task['id']}/files", files=upload("ok.pdf", pdf_bytes("Настоящий PDF"), "application/pdf")
    )
    assert resp.status_code == 201, resp.text

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    resp = await a.post(f"/tasks/{task['id']}/files", files=upload("photo.png", png, "image/png"))
    assert resp.status_code == 201

    # текст с NUL-байтами под видом text/plain — отклоняется
    resp = await a.post(
        f"/tasks/{task['id']}/files", files=upload("notes.txt", b"binary\x00data", "text/plain")
    )
    assert resp.status_code == 400


async def test_order_upload_signature_checked(client, seeded):
    """Загрузка документа приказа тоже проверяет содержимое."""
    a = await auth(client, "admin@example.com")
    from datetime import date

    order = (
        await a.post(
            "/orders",
            json={
                "number": "SIG-1", "order_year": date.today().year, "title": "Приказ для проверки сигнатур",
                "order_type": "ADMIN", "issued_on": date.today().isoformat(), "require_all": True,
            },
        )
    ).json()
    resp = await a.post(
        f"/orders/{order['id']}/files", files=upload("fake.pdf", b"#!/bin/sh\necho pwned", "application/pdf")
    )
    assert resp.status_code == 400
    resp = await a.post(
        f"/orders/{order['id']}/files", files=upload("real.pdf", pdf_bytes("Приказ"), "application/pdf")
    )
    assert resp.status_code == 201


# --------------------------------------------------------------- path traversal
async def test_abspath_rejects_neighbour_directory(tmp_path):
    from app.services.storage import LocalFileStorage

    root = tmp_path / "uploads"
    root.mkdir()
    (root / "file.bin").write_bytes(b"x")
    st = LocalFileStorage(root)

    assert st.abspath("file.bin").name == "file.bin"

    # соседний каталог с общим префиксом имени: startswith-проверка его бы пропустила
    evil = tmp_path / "uploads_x"
    evil.mkdir()
    (evil / "secret.txt").write_text("secret")
    with pytest.raises(Exception):
        st.abspath("../uploads_x/secret.txt")

    with pytest.raises(Exception):
        st.abspath("../../etc/passwd")
