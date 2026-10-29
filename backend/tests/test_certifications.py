"""Аттестация и КПК: CRUD, доступы (учитель — только свои), CSV импорт/экспорт, напоминания."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import auth, pdf_bytes, upload

pytestmark = pytest.mark.asyncio


def cert_payload(user_id: str, **kw) -> dict:
    base = {
        "user_id": user_id,
        "kind": "CATEGORY",
        "category": "Высшая",
        "document_no": "приказ № 123-к",
        "awarded_at": "2023-03-15",
        "expires_at": None,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ доступы
async def test_admin_creates_category_record(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/certifications", json=cert_payload(seeded["teacher1@example.com"]))
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["kind"] == "CATEGORY"
    assert row["category"] == "Высшая"
    assert row["state"] == "indefinite"     # expires_at пустой — бессрочно
    assert row["days_left"] is None
    assert row["user"]["full_name"] == "Учитель Первый"


async def test_validation_requires_category_or_title(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.post("/certifications", json=cert_payload(seeded["teacher1@example.com"], category=None))
    assert resp.status_code == 400
    resp = await a.post("/certifications", json=cert_payload(
        seeded["teacher1@example.com"], kind="CPK", title=None))
    assert resp.status_code == 400


async def test_teacher_sees_only_own_records_readonly(client, seeded):
    a = await auth(client, "admin@example.com")
    await a.post("/certifications", json=cert_payload(seeded["teacher1@example.com"]))
    await a.post("/certifications", json=cert_payload(
        seeded["teacher2@example.com"], category="Первая"))

    t1 = await auth(client, "teacher1@example.com")
    mine = (await t1.get("/certifications")).json()
    assert mine["total"] == 1
    assert all(r["user"]["full_name"] == "Учитель Первый" for r in mine["items"])

    # создание/правка учителю запрещены
    assert (await t1.post("/certifications", json=cert_payload(seeded["teacher1@example.com"]))).status_code == 403
    row_id = mine["items"][0]["id"]
    assert (await t1.patch(f"/certifications/{row_id}", json={"category": "Первая"})).status_code == 403
    assert (await t1.delete(f"/certifications/{row_id}")).status_code == 403

    # чужая карточка — 403
    all_rows = (await a.get("/certifications")).json()["items"]
    other = next(r["id"] for r in all_rows if r["user"]["full_name"] == "Учитель Второй")
    assert (await t1.get(f"/certifications/{other}")).status_code == 403


# ------------------------------------------------------- состояния и фильтры
async def test_states_expiring_expired_and_counts(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    today = date.today()
    await a.post("/certifications", json=cert_payload(
        t1, kind="CPK", category=None, title="Цифровая грамотность", hours=72,
        provider="ИРО", expires_at=(today + timedelta(days=20)).isoformat()))
    await a.post("/certifications", json=cert_payload(
        t1, category="Первая", expires_at=(today - timedelta(days=10)).isoformat()))
    await a.post("/certifications", json=cert_payload(
        t1, category="Высшая", expires_at=(today + timedelta(days=400)).isoformat()))

    data = (await a.get("/certifications")).json()
    counts = data["state_counts"]
    assert counts["all"] == 3
    assert counts["expiring"] == 1 and counts["expired"] == 1 and counts["valid"] == 1

    expiring = (await a.get("/certifications", params={"state": "expiring"})).json()
    assert expiring["total"] == 1
    assert expiring["items"][0]["title"] == "Цифровая грамотность"
    assert expiring["items"][0]["days_left"] == 20

    expired = (await a.get("/certifications", params={"state": "expired"})).json()
    assert expired["total"] == 1 and expired["items"][0]["category"] == "Первая"

    by_search = (await a.get("/certifications", params={"search": "Учитель Первый"})).json()
    assert by_search["total"] == 3
    by_kind = (await a.get("/certifications", params={"kind": "CPK"})).json()
    assert by_kind["total"] == 1


# ------------------------------------------------------------ CSV: экспорт/импорт
async def test_csv_export_and_roundtrip_import(client, seeded):
    a = await auth(client, "admin@example.com")
    t1 = seeded["teacher1@example.com"]
    await a.post("/certifications", json=cert_payload(t1))
    await a.post("/certifications", json=cert_payload(
        t1, kind="CPK", category=None, title="Основы ИКТ", hours=36,
        awarded_at="2025-10-20", expires_at="2028-10-20"))

    exported = await a.get("/certifications/export.csv")
    assert exported.status_code == 200
    text = exported.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("ФИО;Тип;Категория")
    assert "Основы ИКТ" in text and "20.10.2025" in text

    # повторный импорт того же файла — строки распознаются и ОБНОВЛЯЮТСЯ, дубли не плодятся
    result = (await a.post("/certifications/import", files=upload("export.csv", exported.content, "text/csv"))).json()
    assert result["created"] == 0 and result["updated"] == 2 and result["failed"] == []
    assert (await a.get("/certifications")).json()["total"] == 2


async def test_csv_import_creates_and_reports_errors(client, seeded):
    a = await auth(client, "admin@example.com")
    csv_text = (
        "ФИО;Тип;Категория;Название курса;Часы;Учебное заведение;Номер документа;"
        "Дата присвоения/окончания;Действует до;Примечание\n"
        "Учитель Первый;Категория;Первая;;;;приказ № 5;01.09.2026;01.09.2031;\n"
        "Учитель Второй;КПК;;Педагогика;108;ИРО;уд. № 9;15.06.2026;;\n"
        "Неexistent Person;Категория;Высшая;;;;;;;\n"
        "Учитель Первый;Телепатия;Высшая;;;;;;;\n"
        "Учитель Первый;Категория;;;;;;;01.09.2026;\n"
    )
    result = (await a.post(
        "/certifications/import",
        files=upload("certs.csv", csv_text.encode("utf-8"), "text/csv"),
    )).json()
    assert result["created"] == 2
    assert len(result["failed"]) == 3
    reasons = " ".join(f["reason"] for f in result["failed"])
    assert "не найден" in reasons and "непонятный тип" in reasons and "Категория" in reasons

    rows = (await a.get("/certifications", params={"search": "Учитель Второй"})).json()
    assert rows["total"] == 1
    assert rows["items"][0]["hours"] == 108


async def test_import_template_available(client, seeded):
    a = await auth(client, "admin@example.com")
    resp = await a.get("/certifications/import/template.csv")
    assert resp.status_code == 200
    assert resp.content.decode("utf-8-sig").startswith("ФИО;Тип")


# ------------------------------------------------------------------ вложение
async def test_attach_scan_file(client, seeded):
    a = await auth(client, "admin@example.com")
    row = (await a.post("/certifications", json=cert_payload(seeded["teacher1@example.com"]))).json()
    resp = await a.post(
        f"/certifications/{row['id']}/file",
        files=upload("удостоверение.pdf", pdf_bytes("Удостоверение КПК"), "application/pdf"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_file"] is True
    assert resp.json()["file_name"] == "удостоверение.pdf"

    download = await a.get(f"/certifications/{row['id']}/file")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")

    removed = await a.delete(f"/certifications/{row['id']}/file")
    assert removed.json()["has_file"] is False


# ------------------------------------------------------------------ напоминания
async def test_certification_reminders_and_digest(client, seeded, db_session):
    from app.services import reminders

    a = await auth(client, "admin@example.com")
    t1_id = seeded["teacher1@example.com"]
    await a.post("/certifications", json=cert_payload(
        t1_id, category="Первая", expires_at=(date.today() + timedelta(days=10)).isoformat()))

    sent = await reminders.remind_certifications(db_session)
    await db_session.commit()
    assert sent >= 1

    t1 = await auth(client, "teacher1@example.com")
    notes = (await t1.get("/notifications")).json()["items"]
    cert_notes = [n for n in notes if n["type"] == "CERT_EXPIRING"]
    assert cert_notes and "категория «первая»" in cert_notes[0]["title"].lower()

    # дедупликация: повторный прогон в тот же день ничего не отправляет
    assert await reminders.remind_certifications(db_session) == 0

    sent = await reminders.cert_digest(db_session)
    await db_session.commit()
    assert sent >= 1
    admin_notes = (await a.get("/notifications")).json()["items"]
    digests = [n for n in admin_notes if n["type"] == "CERT_DIGEST"]
    assert digests and "Учитель Первый" in digests[0]["body"]


# --------------------------------------------------------------------- журнал
async def test_cert_actions_reach_audit(client, seeded):
    o = await auth(client, "owner@example.com")
    row = (await o.post("/certifications", json=cert_payload(seeded["teacher1@example.com"]))).json()
    await o.patch(f"/certifications/{row['id']}", json={"category": "Первая"})

    audit_rows = (await o.get("/audit", params={"per_page": 50})).json()["items"]
    summaries = [r.get("summary") or "" for r in audit_rows]
    assert any("Добавлена запись об аттестации" in s for s in summaries)
    assert any("Изменена запись об аттестации" in s for s in summaries)
