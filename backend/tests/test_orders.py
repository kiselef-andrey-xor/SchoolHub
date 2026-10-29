"""Модуль «Приказы»: жизненный цикл, файлы, фильтрация, ознакомление, RBAC."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio

TODAY = date.today()


def order_payload(**kw) -> dict:
    base = {
        "number": "100-к",
        "order_year": TODAY.year,
        "title": "Тестовый приказ о тестировании",
        "description": "Создан автотестом",
        "order_type": "HR",
        "issued_on": TODAY.isoformat(),
        "ack_deadline": (TODAY + timedelta(days=7)).isoformat(),
        "require_all": True,
    }
    base.update(kw)
    return base


async def create_published(admin, number="101-к", with_file=True) -> dict:
    order = (await admin.post("/orders", json=order_payload(number=number))).json()
    if with_file:
        resp = await admin.post(
            f"/orders/{order['id']}/files",
            files=upload("prikaz.pdf", pdf_bytes("Приказ №" + number), "application/pdf"),
        )
        assert resp.status_code == 201, resp.text
        order = resp.json()
    resp = await admin.post(f"/orders/{order['id']}/publish")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ создание
async def test_teacher_cannot_create_or_edit_orders(client, seeded):
    t = await auth(client, "teacher1@example.com")
    assert (await t.post("/orders", json=order_payload())).status_code == 403

    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="200-к"))).json()

    assert (await t.patch(f"/orders/{order['id']}", json={"title": "Взлом названия"})).status_code == 403
    assert (await t.post(f"/orders/{order['id']}/publish")).status_code == 403
    assert (await t.delete(f"/orders/{order['id']}")).status_code == 403
    assert (
        await t.post(f"/orders/{order['id']}/files", files=upload("x.pdf", pdf_bytes(), "application/pdf"))
    ).status_code == 403


async def test_draft_invisible_for_teacher_until_published(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="201-к"))).json()
    assert order["status"] == "DRAFT"
    assert order["can_manage"] is True

    t = await auth(client, "teacher1@example.com")
    assert (await t.get(f"/orders/{order['id']}")).status_code == 403
    items = (await t.get("/orders")).json()["items"]
    assert all(o["id"] != order["id"] for o in items)

    # админ видит черновик
    admin_items = (await a.get("/orders", params={"status": "DRAFT"})).json()["items"]
    assert order["id"] in {o["id"] for o in admin_items}


async def test_publish_requires_file(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="202-к"))).json()
    resp = await a.post(f"/orders/{order['id']}/publish")
    assert resp.status_code == 409
    assert "документ" in resp.json()["detail"].lower()


async def test_duplicate_number_conflict(client, seeded):
    a = await auth(client, "admin@example.com")
    assert (await a.post("/orders", json=order_payload(number="203-к"))).status_code == 201
    resp = await a.post("/orders", json=order_payload(number="203-к"))
    assert resp.status_code == 409
    # тот же номер, другой год — можно
    assert (
        await a.post("/orders", json=order_payload(number="203-к", order_year=TODAY.year - 1))
    ).status_code == 201


async def test_file_upload_validation(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="204-к"))).json()
    url = f"/orders/{order['id']}/files"

    assert (await a.post(url, files=upload("virus.exe", b"MZ\x90\x00", "application/x-msdownload"))).status_code == 400
    assert (await a.post(url, files=upload("empty.pdf", b"", "application/pdf"))).status_code == 400

    resp = await a.post(url, files=upload("prikaz.pdf", pdf_bytes(), "application/pdf"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["files"][0]["size_bytes"] > 0
    assert len(body["files"][0]["checksum"]) == 64
    assert body["current_file"]["original_name"] == "prikaz.pdf"


async def test_file_versioning_and_download(client, seeded):
    a = await auth(client, "admin@example.com")
    # версии живут в черновике: у опубликованного приказа документ неизменяем
    order = (await a.post("/orders", json=order_payload(number="205-к"))).json()
    resp = await a.post(
        f"/orders/{order['id']}/files",
        files=upload("prikaz-v1.pdf", pdf_bytes("Версия 1"), "application/pdf"),
    )
    assert resp.status_code == 201, resp.text
    file_v1 = resp.json()["current_file"]

    resp = await a.post(
        f"/orders/{order['id']}/files",
        files=upload("prikaz-v2.pdf", pdf_bytes("Версия 2"), "application/pdf"),
    )
    order = resp.json()
    assert order["current_file"]["version"] == 2
    assert order["current_file"]["id"] != file_v1["id"]
    assert {f["version"] for f in order["files"]} == {1, 2}

    assert (await a.post(f"/orders/{order['id']}/publish")).status_code == 200
    # опубликован: заменить документ нельзя
    resp = await a.post(
        f"/orders/{order['id']}/files",
        files=upload("prikaz-v3.pdf", pdf_bytes("Версия 3"), "application/pdf"),
    )
    assert resp.status_code == 409

    # скачивание текущей версии — PDF inline
    dl = await a.get(f"/orders/{order['id']}/file", params={"inline": "true"})
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/pdf")
    assert "inline" in dl.headers["content-disposition"]
    assert dl.content.startswith(b"%PDF")

    # конкретная версия
    dl1 = await a.get(f"/orders/{order['id']}/file", params={"file_id": file_v1["id"], "inline": "false"})
    assert "attachment" in dl1.headers["content-disposition"]

    # учитель тоже может скачать опубликованный приказ
    t = await auth(client, "teacher1@example.com")
    assert (await t.get(f"/orders/{order['id']}/file")).status_code == 200

    # удалить файл опубликованного приказа нельзя
    resp = await a.delete(f"/orders/{order['id']}/files/{file_v1['id']}")
    assert resp.status_code == 409


async def test_docx_supported(client, seeded):
    import docx as docx_lib  # noqa: F401

    from scripts.demo_docs import DemoDoc, build_docx

    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="206-к", order_type="EDUCATION"))).json()
    payload = build_docx(DemoDoc(number="206-к", title="Приказ о ВПР", preamble="Основание...", items=["Пункт 1"]))
    assert payload is not None
    resp = await a.post(
        f"/orders/{order['id']}/files",
        files=upload("prikaz.docx", payload,
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert resp.status_code == 201, resp.text
    preview = (await a.get(f"/orders/{order['id']}/preview")).json()
    # без установленного LibreOffice превью DOCX = download
    assert preview["preview"] in ("inline", "download")


# --------------------------------------------------------------- ознакомление
async def test_acknowledgement_flow_for_teacher(client, seeded):
    a = await auth(client, "admin@example.com")
    order = await create_published(a, number="301-к")
    # круг ознакомления: админ + 2 учителя (владелец как издатель в круг не входит)
    assert order["ack_total"] == 3
    assert order["ack_done"] == 0

    t = await auth(client, "teacher1@example.com")
    mine = (await t.get("/orders", params={"mine_pending": "true"})).json()["items"]
    assert order["id"] in {o["id"] for o in mine}

    detail = (await t.get(f"/orders/{order['id']}")).json()
    assert detail["my_ack"]["status"] == "PENDING"

    resp = await t.post(f"/orders/{order['id']}/acknowledgements", json={"comment": "Ознакомлен, вопросов нет"})
    assert resp.status_code == 200, resp.text
    ack = resp.json()
    assert ack["status"] == "ACKNOWLEDGED"
    assert ack["acknowledged_at"] is not None
    assert ack["ip_address"]                       # зафиксирован IP
    assert ack["user"]["email"] == "teacher1@example.com"

    # повторное подтверждение невозможно
    again = await t.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert again.status_code == 409

    # в списке «ожидают моего ознакомления» приказа больше нет
    mine_after = (await t.get("/orders", params={"mine_pending": "true"})).json()["items"]
    assert order["id"] not in {o["id"] for o in mine_after}
    acked = (await t.get("/orders", params={"ack": "acknowledged"})).json()["items"]
    assert order["id"] in {o["id"] for o in acked}

    # прогресс виден в карточке
    detail = (await t.get(f"/orders/{order['id']}")).json()
    assert detail["ack_done"] == 1 and detail["ack_total"] == 3

    # лист ознакомления доступен только staff
    assert (await t.get(f"/orders/{order['id']}/acknowledgements")).status_code == 403
    sheet = (await a.get(f"/orders/{order['id']}/acknowledgements")).json()
    assert len(sheet) == 3
    done = [s for s in sheet if s["status"] == "ACKNOWLEDGED"]
    assert len(done) == 1 and done[0]["user"]["email"] == "teacher1@example.com"
    assert done[0]["comment"] == "Ознакомлен, вопросов нет"
    assert done[0]["ip_address"] and done[0]["acknowledged_at"]

    summary = (await a.get(f"/orders/{order['id']}/ack-summary")).json()
    assert summary["total"] == 3 and summary["acknowledged"] == 1
    assert summary["pending"] == 2 and summary["progress"] == 33.3


async def test_cannot_acknowledge_draft_or_archived(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="302-к"))).json()
    await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes(), "application/pdf"))

    t = await auth(client, "teacher1@example.com")
    assert (await t.post(f"/orders/{order['id']}/acknowledgements", json={})).status_code in (403, 409)

    await a.post(f"/orders/{order['id']}/publish")
    archived = await a.post(f"/orders/{order['id']}/archive")
    assert archived.json()["status"] == "ARCHIVED"
    # архивный приказ для учителя недоступен вовсе (row-level доступ: только PUBLISHED)
    assert (await t.get(f"/orders/{order['id']}")).status_code == 403
    assert (await t.post(f"/orders/{order['id']}/acknowledgements", json={})).status_code == 403
    # админ тоже не может собирать ознакомления с архивного документа
    assert (await a.post(f"/orders/{order['id']}/acknowledgements", json={})).status_code == 409


async def test_change_recipients_after_publish(client, seeded):
    a = await auth(client, "admin@example.com")
    order = await create_published(a, number="303-к")
    assert order["ack_total"] == 3

    t2_id = seeded["teacher2@example.com"]
    resp = await a.put(f"/orders/{order['id']}/recipients", json={"user_ids": [t2_id]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ack_total"] == 1

    t1 = await auth(client, "teacher1@example.com")
    # t1 исключён из круга ознакомления
    detail = (await t1.get(f"/orders/{order['id']}")).json()
    assert detail["my_ack"] is None
    assert detail["ack_total"] == 1
    assert (await t1.post(f"/orders/{order['id']}/acknowledgements", json={})).status_code == 409

    t2 = await auth(client, "teacher2@example.com")
    resp2 = await t2.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "ACKNOWLEDGED"


async def test_owner_can_delete_archived_order(client, seeded):
    a = await auth(client, "admin@example.com")
    o = await auth(client, "owner@example.com")
    t = await auth(client, "teacher1@example.com")

    order = await create_published(a, number="505-к")
    assert (await a.post(f"/orders/{order['id']}/archive")).status_code == 200

    # учитель и администрация удалять архив не могут
    assert (await t.delete(f"/orders/{order['id']}")).status_code == 403
    assert (await a.delete(f"/orders/{order['id']}")).status_code == 403

    # опубликованный (не архивный) приказ не удаляет даже Владелец
    published = await create_published(a, number="506-к")
    assert (await o.delete(f"/orders/{published['id']}")).status_code == 409

    # Владелец удаляет архивный приказ безвозвратно
    resp = await o.delete(f"/orders/{order['id']}")
    assert resp.status_code == 204, resp.text
    assert (await o.get(f"/orders/{order['id']}")).status_code == 404

    # лист ознакомления удалён каскадно
    sheet = await o.get(f"/orders/{order['id']}/acknowledgements")
    assert sheet.status_code == 404


async def test_archive_blocks_editing(client, seeded):
    a = await auth(client, "admin@example.com")
    order = await create_published(a, number="304-к")
    # опубликованный приказ неизменяем: только просмотр и ознакомление
    assert (await a.patch(f"/orders/{order['id']}", json={"title": "Новое название"})).status_code == 409
    assert (
        await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes(), "application/pdf"))
    ).status_code == 409

    assert (await a.post(f"/orders/{order['id']}/archive")).status_code == 200
    assert (await a.patch(f"/orders/{order['id']}", json={"title": "Ещё раз"})).status_code == 409
    assert (
        await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes(), "application/pdf"))
    ).status_code == 409
    assert (await a.delete(f"/orders/{order['id']}")).status_code == 403   # архивные удаляет только Владелец
    assert (await a.post(f"/orders/{order['id']}/publish")).status_code == 409

    # архив остаётся доступен для чтения
    t = await auth(client, "teacher1@example.com")
    assert (await t.get(f"/orders/{order['id']}")).status_code == 403       # учитель видит только PUBLISHED
    assert (await a.get(f"/orders/{order['id']}")).status_code == 200


# ------------------------------------------------------------------ фильтрация
async def test_filters_by_type_date_and_search(client, seeded):
    a = await auth(client, "admin@example.com")
    await create_published(a, number="401-к")
    await create_published(
        a,
        number="402",
    )
    resp = await a.post(
        "/orders",
        json=order_payload(
            number="403",
            order_type="SAFETY",
            issued_on=(TODAY - timedelta(days=60)).isoformat(),
            title="Приказ о пожарной безопасности",
        ),
    )
    o3 = resp.json()
    await a.post(f"/orders/{o3['id']}/files", files=upload("pb.pdf", pdf_bytes(), "application/pdf"))
    await a.post(f"/orders/{o3['id']}/publish")

    by_type = (await a.get("/orders", params={"order_type": "SAFETY"})).json()
    assert by_type["total"] == 1
    assert by_type["items"][0]["number"] == "403"

    by_date = (await a.get("/orders", params={
        "date_from": (TODAY - timedelta(days=90)).isoformat(),
        "date_to": (TODAY - timedelta(days=30)).isoformat(),
    })).json()
    assert {o["number"] for o in by_date["items"]} == {"403"}

    by_search = (await a.get("/orders", params={"search": "пожарн"})).json()
    assert by_search["total"] == 1

    by_number = (await a.get("/orders", params={"search": "401-к"})).json()
    assert by_number["total"] == 1

    # некорректный диапазон
    assert (await a.get("/orders", params={"date_from": TODAY.isoformat(), "date_to": (TODAY - timedelta(days=1)).isoformat()})).status_code == 400

    # пагинация
    page = (await a.get("/orders", params={"page": 1, "per_page": 2})).json()
    assert len(page["items"]) == 2 and page["pages"] == 2 and page["total"] == 3


async def test_status_filter_rejected_for_teacher(client, seeded):
    a = await auth(client, "admin@example.com")
    await create_published(a, number="405-к")

    t = await auth(client, "teacher1@example.com")
    # явный 403: учитель не может выбирать статус (видит только опубликованные)
    assert (await t.get("/orders", params={"status": "DRAFT"})).status_code == 403
    assert (await t.get("/orders", params={"status": "ARCHIVED"})).status_code == 403
    # без фильтра — видит опубликованный приказ
    resp = await t.get("/orders")
    assert resp.status_code == 200 and resp.json()["total"] == 1
    assert resp.json()["items"][0]["status"] == "PUBLISHED"
