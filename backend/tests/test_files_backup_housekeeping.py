"""«Файлы школы» (сводка / ZIP / восстановление), поиск по № регистрации,
штрихкод инвентаря и housekeeping (автоочистка истёкших сессий)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.conftest import auth

pytestmark = pytest.mark.asyncio


async def _pep_form(client, email: str):
    """Привязка ПЭП (код+слово) и форма multipart для эндпоинтов восстановления файлов."""
    from tests.test_totp_pep import sign

    _, sig = await sign(client, email)
    s = sig()
    return {"pep_code": s["code"], "pep_word": s["word"]}


def _put_file(root, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _make_storage_state(seeded, db_session):
    """Два файла на диске: один со ссылкой из БД (avatar владельца), один «сирота»;
    плюс битая ссылка (avatar админа на несуществующий путь)."""
    from app.services import storage as storage_module

    root = storage_module.storage.root
    _put_file(root, "2024/10/aaaa.pdf", b"%PDF-1.4 order doc")
    _put_file(root, "2024/10/bbbb.docx", b"PK orphan doc")
    _put_file(root, "2024/01/missing.pdf", b"")          # будет удалён ниже
    (root / "2024/01/missing.pdf").unlink()

    from app.models.user import User

    owner = await db_session.get(User, seeded["owner@example.com"])
    admin = await db_session.get(User, seeded["admin@example.com"])
    owner.avatar = "2024/10/aaaa.pdf"
    admin.avatar = "2024/01/missing.pdf"
    await db_session.commit()
    return root


async def test_files_summary_owner_only(client, seeded, db_session):
    await _make_storage_state(seeded, db_session)

    teacher = await auth(client, "teacher1@example.com")
    assert (await teacher.get("/system/files-summary")).status_code == 403

    owner = await auth(client, "owner@example.com")
    resp = await owner.get("/system/files-summary")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["files_on_disk"] == 2
    assert data["db_references"] == 2
    assert [b["storage_path"] for b in data["broken_links"]] == ["2024/01/missing.pdf"]
    assert data["orphan_files_total"] == 1


async def test_files_zip_and_restore_roundtrip(client, seeded, db_session):
    root = await _make_storage_state(seeded, db_session)
    owner = await auth(client, "owner@example.com")
    form = await _pep_form(owner, "owner@example.com")

    teacher = await auth(client, "teacher1@example.com")
    assert (await teacher.get("/system/files-zip")).status_code == 403

    # архив файлов — выгрузка ВСЕХ документов школы: только по одноразовому билету,
    # подписанному ПЭП «код + слово» (review/REVIEW.md §P1-4)
    assert (await owner.get("/system/files-zip")).status_code == 403
    tresp = await owner.post("/system/export-ticket", json={"target": "files_zip", **form})
    assert tresp.status_code == 200, tresp.text
    ticket = tresp.json()["ticket"]

    resp = await owner.get("/system/files-zip", params={"ticket": ticket})
    assert resp.status_code == 200, resp.text
    payload = resp.content
    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = set(zf.namelist())
    assert "manifest.json" in names
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["version"] == 1
    assert {f["storage_path"] for f in manifest["files"]} == {"2024/10/aaaa.pdf", "2024/10/bbbb.docx"}
    assert all(f["zip_name"].startswith("files/") for f in manifest["files"])
    # исходные имена в архиве — без каталогов хранилища (аватары — с префиксом avatar-)
    assert "files/bbbb.docx" in names
    assert any(n.startswith("files/avatar-") for n in names)

    # билет ОДНОРАЗОВЫЙ: повторное предъявление отклоняется
    assert (await owner.get("/system/files-zip", params={"ticket": ticket})).status_code == 403

    # --- восстановление: удаляем файл -> restored; ещё раз -> already; меняем -> replaced
    (root / "2024/10/aaaa.pdf").unlink()
    resp = await owner.post("/system/files-restore", data=form, files={"file": ("arch.zip", payload, "application/zip")})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["restored"] == ["2024/10/aaaa.pdf"]
    assert report["already"] == ["2024/10/bbbb.docx"]
    assert (root / "2024/10/aaaa.pdf").read_bytes() == b"%PDF-1.4 order doc"

    resp = await owner.post("/system/files-restore", data=form, files={"file": ("arch.zip", payload, "application/zip")})
    report = resp.json()
    assert report["restored"] == [] and len(report["already"]) == 2

    (root / "2024/10/bbbb.docx").write_bytes(b"PK changed version")
    resp = await owner.post("/system/files-restore", data=form, files={"file": ("arch.zip", payload, "application/zip")})
    report = resp.json()
    assert report["replaced"] == ["2024/10/bbbb.docx"]
    assert (root / "2024/10/bbbb.docx").read_bytes() == b"PK orphan doc"


async def test_files_restore_rejects_without_manifest(client, seeded, db_session):
    owner = await auth(client, "owner@example.com")
    # без подписи «код + слово» операция блокируется ещё до чтения архива
    resp = await owner.post("/system/files-restore", files={"file": ("arch.zip", b"PK", "application/zip")})
    assert resp.status_code == 409, resp.text
    form = await _pep_form(owner, "owner@example.com")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("files/whatever.pdf", b"%PDF-1.4")
    resp = await owner.post("/system/files-restore", data=form, files={"file": ("arch.zip", buf.getvalue(), "application/zip")})
    assert resp.status_code == 400
    assert "manifest.json" in resp.json()["detail"]


async def test_files_restore_zipslip_entries_dropped(client, seeded, db_session):
    root = await _make_storage_state(seeded, db_session)
    owner = await auth(client, "owner@example.com")
    form = await _pep_form(owner, "owner@example.com")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("files/ok.pdf", b"%PDF-1.4 ok")
        zf.writestr("files/evil.pdf", b"evil")
        zf.writestr("readme.txt", b"not described")
        manifest = {
            "version": 1,
            "files": [
                {"storage_path": "../../evil.pdf", "zip_name": "files/evil.pdf", "sha256": "x", "size_bytes": 4},
                {"storage_path": "2024/11/ok.pdf", "zip_name": "files/ok.pdf", "sha256": "x", "size_bytes": 11},
            ],
        }
        zf.writestr("manifest.json", json.dumps(manifest))
    resp = await owner.post("/system/files-restore", data=form, files={"file": ("arch.zip", buf.getvalue(), "application/zip")})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["restored"] == ["2024/11/ok.pdf"]
    assert len(report["errors"]) == 2            # zip-slip + файл вне files/ без манифеста
    assert "zip-slip" in report["errors"][0]["reason"]
    assert report["orphans"] == []
    # файл из манифеста с чужим путём НЕ создан вне хранилища
    assert not (root.parent / "evil.pdf").exists()
    assert not (root / "evil.pdf").exists()


async def test_files_restore_streams_member_in_chunks(client, seeded, db_session, monkeypatch):
    """Восстановление пишет файл кусками: большой документ не должен целиком
    помещаться в RAM (лимит памяти контейнера), а после работы — не оставаться
    недописанных `.part`."""
    from app.services import files_backup

    monkeypatch.setattr(files_backup, "CHUNK_SIZE", 5)     # много итераций на мелком файле
    root = await _make_storage_state(seeded, db_session)

    body = b"%PDF-1.4 " + bytes(range(256)) * 3
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("files/big.pdf", body)
        zf.writestr("manifest.json", json.dumps({
            "version": files_backup.MANIFEST_VERSION,
            "app": "schoolhub",
            "files": [{
                "storage_path": "2025/03/big.pdf",
                "zip_name": "files/big.pdf",
                "original_name": "big.pdf",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }],
        }))

    owner = await auth(client, "owner@example.com")
    form = await _pep_form(owner, "owner@example.com")
    resp = await owner.post(
        "/system/files-restore", data=form, files={"file": ("arch.zip", buf.getvalue(), "application/zip")}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["restored"] == ["2025/03/big.pdf"], report
    assert report["errors"] == [], report
    assert (root / "2025/03/big.pdf").read_bytes() == body
    assert list(root.rglob("*.part")) == []

    # повторный прогон — содержимое совпало, файл не перезаписывается
    resp = await owner.post(
        "/system/files-restore", data=form, files={"file": ("arch.zip", buf.getvalue(), "application/zip")}
    )
    assert resp.json()["already"] == ["2025/03/big.pdf"], resp.text


async def test_files_restore_rejects_oversized_member(client, seeded, db_session, monkeypatch):
    """Член архива больше лимита отклоняется поштучно и не оставляет мусора на диске."""
    from app.config import settings
    from app.services import files_backup

    monkeypatch.setattr(files_backup, "CHUNK_SIZE", 4)
    # max_entry = max_upload_mb × 4 МБ → при 0.00001 МБ лимит ~42 байта
    monkeypatch.setattr(settings, "max_upload_mb", 0.00001)
    root = await _make_storage_state(seeded, db_session)

    body = b"x" * 500
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("files/huge.pdf", body)
        zf.writestr("manifest.json", json.dumps({
            "version": files_backup.MANIFEST_VERSION,
            "files": [{"storage_path": "2025/04/huge.pdf", "zip_name": "files/huge.pdf",
                       "original_name": "huge.pdf", "size_bytes": len(body), "sha256": "x"}],
        }))

    owner = await auth(client, "owner@example.com")
    form = await _pep_form(owner, "owner@example.com")
    resp = await owner.post(
        "/system/files-restore", data=form, files={"file": ("arch.zip", buf.getvalue(), "application/zip")}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["restored"] == [], report
    assert report["errors"] and "больше лимита" in report["errors"][0]["reason"], report
    assert not (root / "2025/04/huge.pdf").exists()
    assert list(root.rglob("*.part")) == []


async def test_search_sees_registration_number(client, seeded, db_session):
    from app.models.incoming import IncomingLetter

    db_session.add(
        IncomingLetter(
            reg_year=2026,
            reg_no=77,
            incoming_date=datetime(2026, 10, 1).date(),
            incoming_number="Вх-100/5",
            subject="Письмо о проведении олимпиады",
        )
    )
    await db_session.commit()

    owner = await auth(client, "owner@example.com")
    for q in ("77", "77/2026", "77/20"):
        resp = await owner.get("/search", params={"q": q})
        assert resp.status_code == 200
        hits = resp.json()["incoming"]
        assert hits, f"запрос {q!r} не нашёл номер регистрации"
        assert "77/2026" in hits[0]["title"]


async def test_equipment_barcode_scan_and_uniqueness(client, seeded):
    owner = await auth(client, "owner@example.com")
    resp = await owner.post(
        "/equipment",
        json={"name": "ПЭВМ RAY S101 G1R", "inventory_number": "202410134004486", "barcode": "2000000044323", "quantity": 1},
    )
    assert resp.status_code == 201, resp.text

    resp = await owner.get("/equipment/scan", params={"code": "2000000044323"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "ПЭВМ RAY S101 G1R"

    # инвентарный номер тоже сканируется (аппаратный сканер без штрихкода)
    resp = await owner.get("/equipment/scan", params={"code": "202410134004486"})
    assert resp.status_code == 200

    resp = await owner.get("/equipment/scan", params={"code": "0000000000000"})
    assert resp.status_code == 404

    resp = await owner.post("/equipment", json={"name": "Дубликат", "barcode": "2000000044323"})
    assert resp.status_code == 400
    assert "штрихкод" in resp.json()["detail"].lower()

    # глобальный поиск видит штрихкод
    resp = await owner.get("/search", params={"q": "2000000044323"})
    assert resp.json()["equipment"], "поиск не нашёл позицию по штрихкоду"


async def test_housekeeping_purges_expired_sessions(client, seeded, db_session):
    from app.models.session import RefreshSession
    from app.services import housekeeping

    now = datetime.now(timezone.utc)
    db_session.add_all([
        RefreshSession(
            user_id=seeded["owner@example.com"], family_id=uuid4(), jti="old-jti",
            expires_at=now - timedelta(days=40),
        ),
        RefreshSession(
            user_id=seeded["owner@example.com"], family_id=uuid4(), jti="fresh-jti",
            expires_at=now + timedelta(days=1),
        ),
    ])
    await db_session.commit()

    report = await housekeeping.run_housekeeping(db_session)
    await db_session.commit()
    assert report["sessions_purged"] == 1

    remaining = [r.jti for r in (await db_session.execute(select(RefreshSession))).scalars().all()]
    assert remaining == ["fresh-jti"]


async def test_dashboard_birthdays_widget(client, seeded, db_session):
    from datetime import date

    from app.models.user import User

    today = date.today()
    u = await db_session.get(User, seeded["teacher1@example.com"])
    u.birth_date = date(today.year - 40, today.month, today.day)   # сегодня празднует
    await db_session.commit()

    owner = await auth(client, "owner@example.com")
    resp = await owner.get("/dashboard")
    assert resp.status_code == 200
    births = resp.json()["birthdays"]
    assert any(b["user_id"] == seeded["teacher1@example.com"] and b["days_until"] == 0 for b in births)

    # учителя чужие дни рождения не видят
    teacher = await auth(client, "teacher2@example.com")
    assert (await teacher.get("/dashboard")).json()["birthdays"] == []
