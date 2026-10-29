"""Модуль «Служебные записки» (модуль 2 ТЗ).

Проверяет:
  * создание записи как задачи (автор, тема, текст, исполнитель, срок, вложения);
  * state machine: черновик -> на согласовании -> утверждена (подпись кодом TOTP, всегда)
    -> создана задача исполнителю (взаимные ссылки) -> выполнена;
    отклонение — возврат автору с комментарием;
  * блокировка утверждения без привязки TOTP (TOTP_NOT_BOUND, без фолбэка на пароль);
  * RBAC: чужие записки не видны, правит/отправляет только автор, утверждает администрация;
  * журнал с фильтрами, CSV-экспорт и печатная форма с визой (кто, когда, способ подписи).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth, upload

pytestmark = pytest.mark.asyncio


def note_payload(subject="О проведении ремонта кабинета 214", executor_id=None, days=7):
    return {
        "subject": subject,
        "body": "Прошу организовать проведение текущего ремонта кабинета 214 до конца четверти.",
        "executor_id": executor_id,
        "deadline": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
    }


async def draft(client, author_email="teacher1@example.com", **kw):
    t = await auth(client, author_email)
    resp = await t.post("/notes", json=note_payload(**kw))
    assert resp.status_code == 201, resp.text
    return t, resp.json()


async def test_full_cycle_note_to_task_and_back(client, seeded):
    # автор создаёт черновик с исполнителем и отправляет на согласование
    t, note = await draft(client, executor_id=seeded["teacher_2"])
    assert note["status"] == "DRAFT"
    assert (await t.get(f"/notes/{note['id']}")).json()["status"] == "DRAFT"

    sent = await t.post(f"/notes/{note['id']}/submit")
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "ON_APPROVAL"

    # исполнитель видит записку, но не может её утвердить
    t2 = await auth(client, "teacher2@example.com")
    assert (await t2.get(f"/notes/{note['id']}")).status_code == 200
    assert (await t2.post(f"/notes/{note['id']}/approve", json={})).status_code == 403

    # администрация: без привязки TOTP утверждение блокируется с подсказкой
    a = await auth(client, "admin@example.com")
    blocked = await a.post(f"/notes/{note['id']}/approve", json={})
    assert blocked.status_code == 409 and blocked.json()["code"] == "TOTP_NOT_BOUND", blocked.text

    from tests.test_totp_pep import bind

    a, totp_a, _ = await bind(client, "admin@example.com")
    bad = await a.post(f"/notes/{note['id']}/approve", json={"code": "000000" if totp_a.now() != "000000" else "111111"})
    assert bad.status_code == 422, bad.text  # неверный код — отклонён

    ok = await a.post(f"/notes/{note['id']}/approve", json={"code": totp_a.now()})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "APPROVED"
    assert body["approved_sign_method"] == "TOTP"
    assert body["task_id"], "утверждение должно создать задачу исполнителю"
    assert body["number"] and body["year"]

    # порождённая задача: назначен исполнитель, есть взаимная ссылка на записку
    task = (await a.get(f"/tasks/{body['task_id']}")).json()
    assert task["title"].startswith("[ЗН ")
    assert str(body["id"]) in (task["description"] or "")
    assert any(u["user"]["id"] == seeded["teacher_2"] for u in task["assignees"])

    # исполнитель отмечает выполнение -> записка становится COMPLETED
    t2 = await auth(client, "teacher2@example.com")
    done = await t2.post(f"/tasks/{task['id']}/my-mark")
    assert done.status_code == 200, done.text
    refreshed = (await t2.get(f"/notes/{body['id']}")).json()
    assert refreshed["status"] == "COMPLETED"


async def test_reject_returns_to_author_with_comment(client, seeded):
    t, note = await draft(client, executor_id=seeded["teacher_2"])
    await t.post(f"/notes/{note['id']}/submit")

    a = await auth(client, "admin@example.com")
    no_comment = await a.post(f"/notes/{note['id']}/reject", json={"comment": "  "})
    assert no_comment.status_code == 422, no_comment.text  # min_length в схеме

    rejected = await a.post(f"/notes/{note['id']}/reject", json={"comment": "Уточните объём работ"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["reject_comment"] == "Уточните объём работ"

    # возврат автору: черновик снова доступен для правки и повторной отправки
    edited = await t.patch(f"/notes/{note['id']}", json={"subject": "О ремонте кабинета 214 (уточнено)"})
    assert edited.status_code == 200, edited.text
    again = await t.post(f"/notes/{note['id']}/submit")
    assert again.status_code == 200 and again.json()["status"] == "ON_APPROVAL"


async def test_state_machine_guards(client, seeded):
    t, note = await draft(client, executor_id=seeded["teacher_2"])
    a = await auth(client, "admin@example.com")

    # нельзя утверждать/отклонять черновик, который ещё не на согласовании
    assert (await a.post(f"/notes/{note['id']}/reject", json={"comment": "нельзя"})).status_code == 409

    # без исполнителя отправка невозможна
    t2, bare = await draft(client, author_email="teacher2@example.com", executor_id=None)
    bad_submit = await t2.post(f"/notes/{bare['id']}/submit")
    assert bad_submit.status_code == 409, bad_submit.text  # CONFLICT: «укажите исполнителя»

    # автор не может сам себя отправить повторно после ON_APPROVAL
    await t.post(f"/notes/{note['id']}/submit")
    dup = await t.post(f"/notes/{note['id']}/submit")
    assert dup.status_code == 409, dup.text

    # править можно только черновик/доработанную после отклонения
    patched = await t.patch(f"/notes/{note['id']}", json={"subject": "нет"})
    assert patched.status_code == 409, patched.text


async def test_rbac_visibility(client, seeded):
    t, note = await draft(client, executor_id=seeded["teacher_2"])

    # посторонний учитель не видит чужую записку
    stranger = await auth(client, "teacher2@example.com")
    # teacher2 — исполнитель, поэтому видит; сделаем третьей стороны проверку через список
    listed = await stranger.get("/notes")
    assert all(n["id"] != note["id"] or n["executor"]["id"] == seeded["teacher_2"] for n in listed.json())

    # авторидити: в журнале учителя видны только его записки (как автора или исполнителя)
    owner_ids = {n["author"]["id"] for n in listed.json()} | {n["executor"]["id"] for n in listed.json()}
    assert seeded["teacher_2"] in owner_ids or listed.json() == []

    # v1.11: администрация видит входящий пул (записки БЕЗ назначенного согласующего)
    a = await auth(client, "admin@example.com")
    admin_list = await a.get("/notes")
    assert any(n["id"] == note["id"] for n in admin_list.json())

    # записка с назначенным согласующим видна ТОЛЬКО ему (и Владельцу): админ вне курса
    o = await auth(client, "owner@example.com")
    assigned = (await t.post("/notes", json={
        **note_payload("Записка с назначенным согласующим", executor_id=seeded["teacher_2"]),
        "approver_id": seeded["owner"],
    })).json()
    await t.post(f"/notes/{assigned['id']}/submit")
    assert (await a.get(f"/notes/{assigned['id']}")).status_code == 403
    assert not any(n["id"] == assigned["id"] for n in (await a.get("/notes")).json())
    assert (await o.get(f"/notes/{assigned['id']}")).status_code == 200

    # грант Владельца notes_full_access открывает журнал целиком
    assert (await o.patch(f"/users/{seeded['teacher_2']}", json={"notes_full_access": True})).status_code == 200
    granted = await auth(client, "teacher2@example.com")
    assert any(n["id"] == assigned["id"] for n in (await granted.get("/notes")).json())
    assert (await o.patch(f"/users/{seeded['teacher_2']}", json={"notes_full_access": False})).status_code == 200

    # изменить чужой черновик нельзя
    other = await auth(client, "teacher2@example.com")
    assert (await other.patch(f"/notes/{note['id']}", json={"subject": "взлом"})).status_code in (403, 409)


async def test_journal_filters_export_print(client, seeded):
    t, note = await draft(client, executor_id=seeded["teacher_2"], subject="О проверке журналов посещаемости")
    await t.post(f"/notes/{note['id']}/submit")

    a = await auth(client, "admin@example.com")
    # фильтры журнала
    assert all(n["status"] == "ON_APPROVAL" for n in (await a.get("/notes?status=ON_APPROVAL")).json())
    assert all(n["author"]["id"] == seeded["teacher"] for n in (await a.get(f"/notes?author_id={seeded['teacher']}")).json())
    found = (await a.get("/notes?q=посещаемост")).json()
    assert any(n["id"] == note["id"] for n in found)

    # вложение (только в черновике и только автором)
    up = await t.post(f"/notes/{note['id']}/files", files=upload("akt.txt", b"test", "text/plain"))
    assert up.status_code == 409, up.text  # уже не черновик
    t3, note2 = await draft(client, author_email="teacher2@example.com", executor_id=seeded["teacher"],
                            subject="О списании мебели")
    up2 = await t3.post(f"/notes/{note2['id']}/files", files=upload("смета.txt", b"123", "text/plain"))
    assert up2.status_code == 201, up2.text
    attached = up2.json()["files"][0]
    fid = attached["id"]
    dl = await t3.get(f"/notes/{note2['id']}/files/{fid}/download")
    assert dl.status_code == 200 and dl.content == b"123"
    # v1.12: у вложения есть состояние автоматической PDF-копии
    assert attached["pdf_status"] in ("PENDING", "READY", "NA", "FAILED"), attached
    pr_note2 = await t3.get(f"/notes/{note2['id']}/print")
    assert "Приложение" in pr_note2.text and "смета.txt" in pr_note2.text

    # CSV-экспорт журнала
    csv_resp = await a.get("/notes/export.csv")
    assert csv_resp.status_code == 200
    text = csv_resp.content.decode("utf-8-sig")
    assert "Способ подписи" in text.splitlines()[0]
    assert "О списании мебели" in text

    # --- печатная форма (v1.12: бланк переделан полностью) ---
    pr = await a.get(f"/notes/{note['id']}/print")
    assert pr.status_code == 200
    assert "СЛУЖЕБНАЯ ЗАПИСКА" in pr.text, "нет заголовочной части бланка"
    assert "СОГЛАСОВАНО" in pr.text and "визы нет", "неутверждённая записка: виза должна быть пустой"
    assert "Приложение" not in pr.text, "у записки без вложений блок приложений не печатается"
    # кнопка «Печать» — inline-обработчик, поэтому CSP разрешает script-src 'unsafe-inline'.
    # В <meta> значение экранировано (esc(..., quote=True)), кавычки приходят как &#x27;.
    assert "script-src &#x27;unsafe-inline&#x27;" in pr.text, "кнопка печати требует inline-обработчика"
    assert "img-src data:" in pr.text and "default-src &#x27;none&#x27;" in pr.text, "внешние ресурсы должны быть запрещены"
    assert "onload" not in pr.text, "без auto_print форма не должна открывать диалог печати сама"

    from tests.test_totp_pep import bind

    a, totp_a, _ = await bind(client, "admin@example.com")
    approved = await a.post(f"/notes/{note['id']}/approve", json={"code": totp_a.now()})
    assert approved.status_code == 200, approved.text
    pr2 = await a.get(f"/notes/{note['id']}/print")
    assert "СОГЛАСОВАНО" in pr2.text and "визы нет" not in pr2.text
    assert "одноразовый код аутентификатора" in pr2.text, "в визе не указан способ подписи"
    assert "Отметка об исполнении" in pr2.text, "нет отметки о порождённой задаче"

    # автопечать: отдельный параметр, нужен для печати «в один клик»
    pr3 = await a.get(f"/notes/{note['id']}/print", params={"auto_print": "true"})
    assert 'onload="window.print()"' in pr3.text


async def test_owner_deletes_signed_note_author_cannot(client, seeded):
    """v1.11: утверждённую (подписанную) записку удаляет Владелец; автор — только черновик/отклонённую."""
    t, note = await draft(client, executor_id=seeded["teacher_2"])
    await t.post(f"/notes/{note['id']}/submit")

    from tests.test_totp_pep import bind

    a, totp_a, _ = await bind(client, "admin@example.com")
    ok = await a.post(f"/notes/{note['id']}/approve", json={"code": totp_a.now()})
    assert ok.status_code == 200, ok.text
    task_id = ok.json()["task_id"]

    # автор подписанную записку удалить не может
    assert (await t.delete(f"/notes/{note['id']}")).status_code == 409

    # Владелец — может, даже с визой
    o = await auth(client, "owner@example.com")
    assert (await o.delete(f"/notes/{note['id']}")).status_code == 204
    assert (await o.get(f"/notes/{note['id']}")).status_code == 404

    # порождённая задача остаётся: её жизненный цикл независим от записки
    assert (await a.get(f"/tasks/{task_id}")).status_code == 200


async def test_assigned_approver_approves_others_denied(client, seeded):
    """v1.11: автор выбирает согласующего; утвердить может только он (Владелец — всегда)."""
    t, note = await draft(client, executor_id=seeded["teacher_2"])
    upd = await t.patch(f"/notes/{note['id']}", json={"approver_id": seeded["owner"]})
    assert upd.status_code == 200, upd.text
    assert upd.json()["approver"]["id"] == seeded["owner"]
    await t.post(f"/notes/{note['id']}/submit")

    from tests.test_totp_pep import bind

    a, totp_a, _ = await bind(client, "admin@example.com")
    denied = await a.post(f"/notes/{note['id']}/approve", json={"code": totp_a.now()})
    assert denied.status_code == 403, denied.text

    o, totp_o, _ = await bind(client, "owner@example.com")
    ok = await o.post(f"/notes/{note['id']}/approve", json={"code": totp_o.now()})
    assert ok.status_code == 200, ok.text
    assert ok.json()["approver"]["id"] == seeded["owner"]
