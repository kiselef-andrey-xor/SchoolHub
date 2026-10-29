"""Журнал событий: человекопонятные формулировки (summary + lines) в /audit."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import DEMO_PASSWORD, auth, new_client, pdf_bytes, upload

pytestmark = pytest.mark.asyncio

TODAY = date.today()


def _order_payload(number: str) -> dict:
    return {
        "number": number,
        "order_year": TODAY.year,
        "title": "О проверке журнала событий",
        "description": "Создан автотестом человекопонятного аудита",
        # HR: политика по умолчанию CLICK — тест проверяет базовый путь ознакомления.
        # (Усиленная ПЭП для приказов по обучающимся покрыта tests/test_totp_pep.py.)
        "order_type": "HR",
        "issued_on": TODAY.isoformat(),
        "ack_deadline": (TODAY + timedelta(days=5)).isoformat(),
        "require_all": True,
    }


async def test_audit_returns_human_summary_and_lines(client, seeded):
    a = await auth(client, "admin@example.com")
    o = await auth(new_client(client), "owner@example.com")
    t = await auth(new_client(client), "teacher1@example.com")

    order = (await a.post("/orders", json=_order_payload("777-к"))).json()
    assert order.get("id"), order
    resp = await a.post(
        f"/orders/{order['id']}/files",
        files=upload("prikaz.pdf", pdf_bytes("Приказ № 777-к"), "application/pdf"),
    )
    assert resp.status_code == 201, resp.text
    assert (await a.post(f"/orders/{order['id']}/publish")).status_code == 200

    ack = await t.post(f"/orders/{order['id']}/acknowledgements", json={"comment": "Прочитал(а) внимательно"})
    assert ack.status_code == 200, ack.text

    # меняем круг ознакомления на одного учителя — в журнале должны появиться имена, а не uuid
    tid = seeded["teacher1@example.com"]
    resp = await a.put(f"/orders/{order['id']}/recipients", json={"user_ids": [tid]})
    assert resp.status_code == 200, resp.text

    logs = (await o.get("/audit", params={"per_page": 100})).json()["items"]
    by_action: dict[str, dict] = {}
    for item in logs:
        by_action.setdefault(item["action"], item)

    created = by_action["ORDER_CREATE"]
    assert created["summary"].startswith("Создан приказ № 777-к"), created
    assert "О проверке журнала событий" in created["summary"]
    # фикстура использует приказ по личному составу (HR) — базовый путь ознакомления;
    # усиленная ПЭП для приказов «по обучающимся» покрыта tests/test_totp_pep.py
    assert "по личному составу" in created["summary"]

    published = by_action["ORDER_PUBLISH"]
    assert "опубликован" in published["summary"]
    assert "получателей" in published["summary"]

    acked = by_action["ORDER_ACK"]
    assert acked["summary"].startswith("Подтверждено ознакомление с приказом № 777-к"), acked
    assert any("Прочитал(а) внимательно" in line for line in acked["lines"]), acked

    recipients = by_action["ORDER_RECIPIENTS"]
    assert "круг ознакомления" in recipients["summary"].lower()
    blob = " ".join(recipients["lines"])
    assert "Учитель" in blob or "учитель" in blob or blob.count("-") == 0
    assert tid not in blob, f"uuid не должен торчать в журнале: {blob}"

    login = by_action["LOGIN"]
    assert login["summary"] == "Вход в систему"
    assert any("Способ входа" in line for line in login["lines"])

    # сырые технические поля не попадают в человекопонятный вид
    for item in logs:
        text = (item["summary"] + " " + " ".join(item["lines"])).lower()
        assert "sha256" not in text
        assert "{'from'" not in text


async def test_audit_human_diff_labels(client, seeded):
    a = await auth(client, "admin@example.com")
    o = await auth(new_client(client), "owner@example.com")

    user = (
        await a.post(
            "/users",
            json={
                "email": "human-audit@example.com",
                "password": DEMO_PASSWORD,
                "full_name": "Аудитов Человеческий",
                "role": "TEACHER",
            },
        )
    ).json()
    assert user.get("id"), user
    resp = await o.patch(f"/users/{user['id']}", json={"role": "ADMIN"})
    assert resp.status_code == 200, resp.text

    items = (await o.get("/audit", params={"action": "USER_UPDATE", "per_page": 10})).json()["items"]
    assert items, "нет записи USER_UPDATE"
    entry = items[0]
    assert entry["summary"].startswith("Изменён пользователь"), entry
    assert any("роль: учитель → администрация" in line for line in entry["lines"]), entry["lines"]
