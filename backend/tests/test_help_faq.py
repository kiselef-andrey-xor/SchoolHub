"""Модуль «Справка и инструкции (FAQ)» (модуль 4 ТЗ).

Проверяет:
  * системные карточки сидируются (пароль, роли, подпись, локдаун), удалять их нельзя;
  * категории, поиск по названию/тексту, фильтрация;
  * CRUD инструкций администрацией (RBAC: учитель — только чтение);
  * флаг «обязательно к прочтению» открывает лист ознакомления (механизм приказов):
    отметка кликом; при системном флаге help_require_code — только ПЭП (TOTP),
    без привязки блокировка TOTP_NOT_BOUND, без фолбэка на пароль;
  * загрузка/скачивание файла инструкции; попадание в глобальный поиск.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio


import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def _session_scope(app):
    from app.database import get_session

    override = app.dependency_overrides[get_session]
    agen = override()
    session = await agen.__anext__()
    try:
        yield session
    finally:
        await agen.aclose()


async def test_system_cards_seeded_and_protected(client, seeded, app):
    from app.services.help import seed_system_articles

    async with _session_scope(app) as s:
        created = await seed_system_articles(s)
        await s.commit()
    assert created > 0
    # повторный сид идемпотентен
    async with _session_scope(app) as s:
        assert await seed_system_articles(s) == 0

    o = await auth(client, "owner@example.com")
    arts = (await o.get("/help?kind=SYSTEM")).json()
    titles = " ".join(a["title"].lower() for a in arts)
    assert arts, "системные карточки должны быть видны"
    assert any(k in titles for k in ("парол", "подпис", "локдаун", "рол")), titles

    target = next(a for a in arts if a["kind"] == "SYSTEM")
    resp = await o.delete(f"/help/{target['id']}")
    assert resp.status_code == 409, resp.text  # системные удалять нельзя


async def test_crud_rbac_categories_search(client, seeded):
    t = await auth(client, "teacher1@example.com")
    forbidden = await t.post("/help", json={"title": "Инструкция по пожарной безопасности", "category": "Безопасность"})
    assert forbidden.status_code == 403, forbidden.text

    a = await auth(client, "admin@example.com")
    art = (await a.post("/help", json={
        "title": "Инструкция по эвакуации",
        "category": "Безопасность",
        "body": "Порядок действий при тревоге: собрать журнал, вывести класс, проверить списки.",
    })).json()
    assert art["kind"] == "UPLOAD" and art["mandatory_read"] is False

    # категории и фильтры
    cats = (await a.get("/help/categories")).json()
    assert "Безопасность" in cats
    assert all(x["category"] == "Безопасность" for x in (await a.get("/help?category=Безопасность")).json())

    # поиск по тексту
    found = (await t.get("/help?q=эвакуац")).json()
    assert any(x["id"] == art["id"] for x in found)
    assert (await t.get("/help?q=несуществующийзапрос")).json() == []

    # правка и удаление своей инструкции
    upd = await a.patch(f"/help/{art['id']}", json={"title": "Инструкция по эвакуации (ред. 2)"})
    assert upd.status_code == 200 and upd.json()["title"].endswith("(ред. 2)")
    teacher_patch = await t.patch(f"/help/{art['id']}", json={"title": "взлом"})
    assert teacher_patch.status_code == 403, teacher_patch.text
    assert (await a.delete(f"/help/{art['id']}")).status_code == 204
    assert (await a.get(f"/help/{art['id']}")).status_code == 404


async def test_mandatory_ack_sheet_click_flow(client, seeded):
    a = await auth(client, "admin@example.com")
    art = (await a.post("/help", json={
        "title": "Правила внутреннего трудового распорядка",
        "category": "Кадры",
        "body": "Ознакомьте всех сотрудников в течение недели.",
        "mandatory_read": True,
    })).json()

    # лист ознакомления создан и виден администрации; учителю — нет
    detail = (await a.get(f"/help/{art['id']}")).json()
    assert detail["acks"], "лист ознакомления должен быть открыт"
    ids = {x["user"]["id"] for x in detail["acks"]}
    assert seeded["teacher"] in ids and seeded["teacher_2"] in ids

    t = await auth(client, "teacher1@example.com")
    mine = (await t.get(f"/help/{art['id']}")).json()
    assert mine["my_ack_at"] is None
    pending = (await t.get("/help/mine/pending")).json()
    assert pending["count"] >= 1

    ok = await t.post(f"/help/{art['id']}/ack", json={})
    assert ok.status_code == 200, ok.text
    assert ok.json()["my_ack_at"] is not None

    # двойная отметка — конфликт
    dup = await t.post(f"/help/{art['id']}/ack", json={})
    assert dup.status_code == 409, dup.text

    sheet = (await a.get(f"/help/{art['id']}")).json()["acks"]
    row = next(r for r in sheet if r["user"]["id"] == seeded["teacher"])
    assert row["sign_method"] == "CLICK" and row["acked_at"]

    # снятие флага «обязательно к прочтению»
    off = await a.patch(f"/help/{art['id']}", json={"mandatory_read": False})
    assert off.status_code == 200, off.text
    ack_off = await t.post(f"/help/{art['id']}/ack", json={})
    assert ack_off.status_code == 409, ack_off.text  # статья больше не обязательна


async def test_help_require_code_flag_forces_totp(client, seeded, app):

    from app.models.system_flag import SystemFlag

    async with _session_scope(app) as s:
        row = await s.get(SystemFlag, "help_require_code")
        if row is None:
            s.add(SystemFlag(key="help_require_code", value="1"))
        else:
            row.value = "1"
        await s.commit()

    a = await auth(client, "admin@example.com")
    art = (await a.post("/help", json={
        "title": "Новый регламент работы с персданными", "category": "Безопасность",
        "body": "Требует ознакомления под электронную подпись.", "mandatory_read": True,
    })).json()

    t = await auth(client, "teacher1@example.com")
    blocked = await t.post(f"/help/{art['id']}/ack", json={})
    assert blocked.status_code == 409 and blocked.json()["code"] == "TOTP_NOT_BOUND", blocked.text

    from tests.test_totp_pep import bind

    t, totp_t, _ = await bind(client, "teacher1@example.com")
    bad = await t.post(f"/help/{art['id']}/ack", json={"code": "000000" if totp_t.now() != "000000" else "111111"})
    assert bad.status_code == 422, bad.text
    ok = await t.post(f"/help/{art['id']}/ack", json={"code": totp_t.now()})
    assert ok.status_code == 200, ok.text

    sheet = (await a.get(f"/help/{art['id']}")).json()["acks"]
    row = next(r for r in sheet if r["user"]["id"] == seeded["teacher"])
    assert row["sign_method"] == "TOTP"


async def test_file_upload_download_and_global_search(client, seeded):
    a = await auth(client, "admin@example.com")
    art = (await a.post("/help", json={
        "title": "Памятка по делопроизводству", "category": "Общее",
        "body": "Файл-инструкция во вложении.",
    })).json()
    up = await a.post(f"/help/{art['id']}/file", files=upload("paper.pdf", pdf_bytes(), "application/pdf"))
    assert up.status_code == 200, up.text
    dl = await a.get(f"/help/{art['id']}/file/download")
    assert dl.status_code == 200 and dl.content.startswith(b"%PDF")

    # попадает в глобальный поиск
    results = (await a.get("/search?q=делопроизводству")).json()
    flat = str(results)
    assert art["id"] in flat or "Памятка по делопроизводству" in flat
