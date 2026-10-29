"""Видимость статей справки/FAQ: «только для администрации» и «только для Владельца».

Проверяет новую возможность модуля 4 (поле `visibility`: ALL / ADMIN / OWNER):
  * статью служебной видимости не видно в списке, в категориях и в глобальном поиске;
  * прямой запрос по id, скачивание файла и отметка «Ознакомлен» отдают 404,
    а не 403 — сам факт существования служебной инструкции не раскрывается;
  * администрация не может задать видимость OWNER (такую статью она сама не увидит);
  * лист ознакомления `mandatory_read` ограничен кругом читателей: у учителя
    не появляется «долг» по инструкции, которой он не видит;
  * смена видимости у обязательной инструкции пересчитывает лист ознакомления;
  * Владелец видит и правит всё, включая статьи «только для Владельца».
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio


async def _create(cli, *, title, visibility="ALL", category="Общее", mandatory=False, body=None):
    resp = await cli.post("/help", json={
        "title": title, "category": category, "body": body or f"Текст инструкции «{title}».",
        "mandatory_read": mandatory, "visibility": visibility,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ доступ
async def test_admin_only_article_hidden_from_teachers(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")
    owner = await auth(client, "owner@example.com")

    art = await _create(admin, title="Регламент согласования приказов", visibility="ADMIN",
                        category="Служебное")
    assert art["visibility"] == "ADMIN"

    # список: администрация и Владелец видят, учитель — нет
    admin_ids = {a["id"] for a in (await admin.get("/help")).json()}
    owner_ids = {a["id"] for a in (await owner.get("/help")).json()}
    teacher_ids = {a["id"] for a in (await teacher.get("/help")).json()}
    assert art["id"] in admin_ids and art["id"] in owner_ids
    assert art["id"] not in teacher_ids

    # карточка по id: 404 (не 403 — не раскрываем факт существования)
    assert (await admin.get(f"/help/{art['id']}")).status_code == 200
    assert (await owner.get(f"/help/{art['id']}")).status_code == 200
    resp = await teacher.get(f"/help/{art['id']}")
    assert resp.status_code == 404, resp.text

    # категории не выдают служебный раздел
    assert "Служебное" in (await admin.get("/help/categories")).json()
    assert "Служебное" not in (await teacher.get("/help/categories")).json()

    # глобальный поиск тоже не находит служебную статью
    hits = (await teacher.get("/search", params={"q": "Регламент"})).json()["help"]
    assert all(h["id"] != art["id"] for h in hits)
    hits = (await admin.get("/search", params={"q": "Регламент"})).json()["help"]
    assert any(h["id"] == art["id"] for h in hits)

    # правка и удаление — только свои по видимости
    assert (await teacher.patch(f"/help/{art['id']}", json={"title": "Взлом"})).status_code == 404
    assert (await teacher.delete(f"/help/{art['id']}")).status_code == 404
    assert (await admin.patch(f"/help/{art['id']}", json={"title": "Регламент согласования (ред. 2)"})).status_code == 200


async def test_owner_only_article_hidden_from_administration(client, seeded):
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    art = await _create(owner, title="Заметки учредителя по кадрам", visibility="OWNER",
                        category="Личное")

    assert art["id"] in {a["id"] for a in (await owner.get("/help")).json()}
    for cli in (admin, teacher):
        assert art["id"] not in {a["id"] for a in (await cli.get("/help")).json()}
        assert (await cli.get(f"/help/{art['id']}")).status_code == 404
    assert "Личное" not in (await admin.get("/help/categories")).json()
    assert "Личное" in (await owner.get("/help/categories")).json()

    # администрация не может ни прочитать, ни изменить, ни удалить
    assert (await admin.patch(f"/help/{art['id']}", json={"visibility": "ALL"})).status_code == 404
    assert (await admin.delete(f"/help/{art['id']}")).status_code == 404

    # Владелец может сделать статью общей — и она сразу видна всем
    resp = await owner.patch(f"/help/{art['id']}", json={"visibility": "ALL"})
    assert resp.status_code == 200 and resp.json()["visibility"] == "ALL"
    assert art["id"] in {a["id"] for a in (await teacher.get("/help")).json()}


async def test_admin_cannot_set_owner_visibility(client, seeded):
    """Администрация выбирает ALL или ADMIN: статья «только для Владельца»
    стала бы невидимой и недоступной для правки её же автору."""
    admin = await auth(client, "admin@example.com")

    resp = await admin.post("/help", json={
        "title": "Попытка скрыть от всех", "category": "Общее", "visibility": "OWNER",
    })
    assert resp.status_code == 403, resp.text

    art = await _create(admin, title="Общая инструкция", visibility="ALL")
    resp = await admin.patch(f"/help/{art['id']}", json={"visibility": "OWNER"})
    assert resp.status_code == 403
    # видимость не изменилась
    assert (await admin.get(f"/help/{art['id']}")).json()["visibility"] == "ALL"
    # сузить до «только администрации» администрация может
    resp = await admin.patch(f"/help/{art['id']}", json={"visibility": "ADMIN"})
    assert resp.status_code == 200 and resp.json()["visibility"] == "ADMIN"


async def test_visibility_filter_query(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")
    await _create(admin, title="Служебная инструкция А", visibility="ADMIN", category="Служебное")
    await _create(admin, title="Общая инструкция Б", visibility="ALL", category="Служебное")

    only_admin = (await admin.get("/help", params={"visibility": "ADMIN"})).json()
    assert only_admin and all(a["visibility"] == "ADMIN" for a in only_admin)
    only_all = (await admin.get("/help", params={"visibility": "ALL"})).json()
    assert only_all and all(a["visibility"] == "ALL" for a in only_all)

    # учителю недоступен даже запрос фильтра ADMIN — и тем более выдача
    assert (await teacher.get("/help", params={"visibility": "ADMIN"})).status_code == 403
    assert (await teacher.get("/help", params={"visibility": "OWNER"})).status_code == 403
    assert (await teacher.get("/help", params={"visibility": "ALL"})).status_code == 200


async def test_teacher_cannot_create_articles(client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.post("/help", json={"title": "Своя инструкция", "visibility": "ALL"})
    assert resp.status_code == 403


# ----------------------------------------------------- лист ознакомления
async def test_mandatory_sheet_follows_visibility(client, seeded):
    """Обязательная инструкция для администрации не создаёт «долг» у учителей."""
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")
    owner = await auth(client, "owner@example.com")

    art = await _create(admin, title="Ежегодная инвентаризация (служебная)",
                        visibility="ADMIN", mandatory=True)

    assert (await admin.get("/help/mine/pending")).json()["count"] >= 1
    assert (await owner.get("/help/mine/pending")).json()["count"] >= 1
    assert (await teacher.get("/help/mine/pending")).json()["count"] == 0

    # лист ознакомления виден администрации и не содержит учителей
    sheet = (await admin.get(f"/help/{art['id']}")).json()["acks"]
    emails = {row["user"]["email"] for row in sheet}
    assert "admin@example.com" in emails and "owner@example.com" in emails
    assert "teacher1@example.com" not in emails

    # учитель не может отметиться в чужом листе
    assert (await teacher.post(f"/help/{art['id']}/ack", json={})).status_code == 404

    resp = await admin.post(f"/help/{art['id']}/ack", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["my_ack_at"] is not None


async def test_visibility_change_rescopes_ack_sheet(client, seeded):
    """Сужение круга читателей убирает зависшие отметки «не ознакомлен»."""
    owner = await auth(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    art = await _create(owner, title="Правила работы с персональными данными",
                        visibility="ALL", mandatory=True)
    assert (await teacher.get("/help/mine/pending")).json()["count"] >= 1
    sheet = (await owner.get(f"/help/{art['id']}")).json()["acks"]
    assert any(row["user"]["email"] == "teacher1@example.com" for row in sheet)

    # сужаем до администрации: у учителя отметка исчезает вместе с «долгом»
    resp = await owner.patch(f"/help/{art['id']}", json={"visibility": "ADMIN"})
    assert resp.status_code == 200 and resp.json()["visibility"] == "ADMIN"

    assert (await teacher.get("/help/mine/pending")).json()["count"] == 0
    assert (await teacher.get(f"/help/{art['id']}")).status_code == 404
    sheet = (await admin.get(f"/help/{art['id']}")).json()["acks"]
    emails = {row["user"]["email"] for row in sheet}
    assert "teacher1@example.com" not in emails
    assert "admin@example.com" in emails

    # расширяем обратно: лист снова открыт для учителей
    resp = await owner.patch(f"/help/{art['id']}", json={"visibility": "ALL"})
    assert resp.status_code == 200
    assert (await teacher.get("/help/mine/pending")).json()["count"] >= 1
    sheet = (await owner.get(f"/help/{art['id']}")).json()["acks"]
    assert any(row["user"]["email"] == "teacher1@example.com" for row in sheet)


# ------------------------------------------------------------------ файл
async def test_file_of_restricted_article_is_not_downloadable(client, seeded):
    admin = await auth(client, "admin@example.com")
    teacher = await auth(client, "teacher1@example.com")

    art = await _create(admin, title="Приложение: форма отчёта", visibility="ADMIN")
    resp = await admin.post(f"/help/{art['id']}/file",
                            files=upload("forma.pdf", pdf_bytes("Форма отчёта"), "application/pdf"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_name"] == "forma.pdf"

    assert (await admin.get(f"/help/{art['id']}/file/download")).status_code == 200
    resp = await teacher.get(f"/help/{art['id']}/file/download")
    assert resp.status_code == 404, resp.text
    # загрузить файл к служебной статье учитель тоже не может
    resp = await teacher.post(f"/help/{art['id']}/file",
                              files=upload("x.pdf", pdf_bytes("x"), "application/pdf"))
    assert resp.status_code == 404


async def test_system_cards_stay_visible_to_everyone(client, seeded, app):
    """Существующие (в том числе системные) статьи по умолчанию общедоступны."""
    from app.database import get_session
    from app.services.help import seed_system_articles

    agen = app.dependency_overrides[get_session]()
    session = await agen.__anext__()
    try:
        await seed_system_articles(session)
        await session.commit()
    finally:
        await agen.aclose()

    teacher = await auth(client, "teacher1@example.com")
    arts = (await teacher.get("/help", params={"kind": "SYSTEM"})).json()
    assert arts
    assert all(a["visibility"] == "ALL" for a in arts)
