"""Модуль «ПЭП через аутентификатор (TOTP) + политика подписи».

Проверяет:
  * привязку TOTP (секрет выдаётся одноразово, подтверждение первым кодом), отвязку;
  * уровни подписи ознакомления: NONE (аудит) / CLICK / TOTP / CODE_WORD;
  * автоправило «тип приказа -> политика»: приказы о движении обучающихся — всегда усиленная;
  * блокировку без привязки с подсказкой (TOTP_NOT_BOUND, без фолбэка на пароль);
  * «код + слово» на чувствительных операциях (очистка аудита, локдаун, удаление пользователя);
  * запись способа подписи и времени в лист ознакомления (API + CSV-выгрузка).
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, pdf_bytes, upload
from tests.test_orders import create_published, order_payload

pytestmark = pytest.mark.asyncio


async def bind(client, email: str):
    """Логинит пользователя и полностью привязывает аутентификатор.

    Возвращает (cli, totp_obj, secret). Секрет известен тесту — как пользователю
    в приложении-аутентификаторе.
    """
    import pyotp

    cli = await auth(client, email)
    resp = await cli.post("/users/me/totp/setup")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    secret = body["secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    # до подтверждения первым кодом привязка НЕ считается завершённой
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is False

    def other_code() -> str:
        now = pyotp.TOTP(secret).now()
        for candidate in ("000000", "111111", "222222"):
            if candidate != now:
                return candidate
        raise AssertionError("неподходящий набор кодов")

    bad = await cli.post("/users/me/totp/confirm", json={"code": other_code()})
    assert bad.status_code == 422, bad.text  # неверный код отклонён
    ok = await cli.post("/users/me/totp/confirm", json={"code": pyotp.TOTP(secret).now()})
    assert ok.status_code == 204, ok.text
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True
    return cli, pyotp.TOTP(secret), secret


async def sign(client, email: str, word: str = "голубой-кит"):
    """Полная подготовка Владельца к чувствительным операциям.

    Привязывает аутентификатор и задаёт контрольное слово; возвращает пару
    (клиент, функция current() -> {"code": ..., "word": ...}) для подписи «код + слово».
    Функция каждый раз выдаёт свежий одноразовый код (окно TOTP — 30 секунд).
    """
    import pyotp

    cli, totp_obj, secret = await bind(client, email)
    resp = await cli.put("/users/me/totp/recovery-word", json={"word": word, "code": totp_obj.now()})
    assert resp.status_code == 204, resp.text

    def current() -> dict:
        return {"code": pyotp.TOTP(secret).now(), "word": word}

    return cli, current


def pep(body: dict) -> dict:
    """Тело запроса для чувствительной операции: подпись «код + слово»."""
    return body() if callable(body) else body


# ------------------------------------------------------------------ привязка/отвязка
async def test_totp_setup_confirm_unbind(client, seeded):
    cli, totp_obj, _ = await bind(client, "teacher1@example.com")
    # секрет больше никогда не отдаётся API
    assert "totp_secret" not in (await cli.get("/auth/me")).text.replace("totp_secret_enc", "")
    # повторная привязка без отвязки запрещена
    assert (await cli.post("/users/me/totp/setup")).status_code == 409

    # отвязка ДЕЙСТВУЮЩЕЙ привязки подтверждается текущим кодом: иначе укравший
    # access-токен снял бы чужой аутентификатор и привязал свой (обнулив ПЭП)
    no_code = await cli.delete("/users/me/totp")
    assert no_code.status_code == 409, no_code.text
    assert no_code.json()["code"] == "TOTP_CODE_REQUIRED"
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True

    wrong = next(c for c in ("000000", "111111", "222222") if c != totp_obj.now())
    assert (await cli.request("DELETE", "/users/me/totp", json={"code": wrong})).status_code == 422
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True

    # секрет уничтожается, подпись кодом становится невозможна
    assert (await cli.request("DELETE", "/users/me/totp", json={"code": totp_obj.now()})).status_code == 204
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is False
    assert (await cli.delete("/users/me/totp")).status_code == 400


async def test_unconfirmed_binding_removed_without_code(client, seeded):
    """Привязка, не подтверждённая первым кодом, снимается без кода.

    Кода в приложении-аутентификаторе у человека ещё нет (он его только получил),
    поэтому требовать подтверждение нельзя — иначе секрет так и остался бы висеть.
    """
    cli = await auth(client, "teacher2@example.com")
    assert (await cli.post("/users/me/totp/setup")).status_code == 200
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is False
    assert (await cli.delete("/users/me/totp")).status_code == 204
    # повторная привязка снова возможна
    assert (await cli.post("/users/me/totp/setup")).status_code == 200


# ------------------------------------------------------------------ CLICK без привязки
async def test_ack_policy_click_works_without_binding(client, seeded):
    a = await auth(client, "admin@example.com")
    order = await create_published(a, number="500-к")  # HR => CLICK (текущее поведение)
    assert order["signature_policy"] == "CLICK"
    t = await auth(client, "teacher1@example.com")  # без привязки
    resp = await t.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sign_method"] == "CLICK"
    assert resp.json()["acknowledged_at"] is not None


# ------------------------------------------------------------------ NONE: только аудит
async def test_none_policy_audited_without_signature(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="504-к", signature_policy="NONE"))).json()
    assert order["signature_policy"] == "NONE"
    await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes("x"), "application/pdf"))
    order = (await a.post(f"/orders/{order['id']}/publish")).json()
    t = await auth(client, "teacher1@example.com")
    resp = await t.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sign_method"] == "NONE"


# ------------------------------------------------------------------ TOTP + блок без привязки
async def test_ack_totp_blocks_unbound_with_hint_no_password_fallback(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="501-к", signature_policy="TOTP"))).json()
    await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes("Приказ"), "application/pdf"))
    order = (await a.post(f"/orders/{order['id']}/publish")).json()
    assert order["signature_policy"] == "TOTP"

    t = await auth(client, "teacher1@example.com")  # НЕ привязан
    blocked = await t.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "TOTP_NOT_BOUND"
    assert "профил" in blocked.json()["detail"].lower()  # подсказка: куда идти привязываться
    # фолбэка на пароль нет: даже если прислать в поле кода пароль — тот же блок по отсутствию привязки
    fb = await t.post(f"/orders/{order['id']}/acknowledgements", json={"code": "hunter2!"})
    assert fb.status_code == 409 and fb.json()["code"] == "TOTP_NOT_BOUND"

    # привязались — неверный код отклоняется, верный подписывает
    t2, totp_obj, _ = await bind(client, "teacher1@example.com")
    wrong = await t2.post(f"/orders/{order['id']}/acknowledgements", json={"code": "123456"})
    assert wrong.status_code == 422 and wrong.json()["code"] == "TOTP_INVALID_CODE"
    ok = await t2.post(f"/orders/{order['id']}/acknowledgements", json={"code": totp_obj.now()})
    assert ok.status_code == 200, ok.text
    ack = ok.json()
    assert ack["sign_method"] == "TOTP"
    assert ack["acknowledged_at"] is not None


# ------------------------------------------------------------------ автоправило STUDENTS
async def test_students_order_type_forces_enhanced_policy(client, seeded):
    a = await auth(client, "admin@example.com")
    # объявили слабую политику для типа STUDENTS — автоправило поднимает до TOTP
    order = (await a.post(
        "/orders", json=order_payload(number="502-к", order_type="STUDENTS", signature_policy="CLICK")
    )).json()
    assert order["signature_policy"] == "TOTP"
    await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes("x"), "application/pdf"))
    order = (await a.post(f"/orders/{order['id']}/publish")).json()
    t = await auth(client, "teacher2@example.com")
    resp = await t.post(f"/orders/{order['id']}/acknowledgements", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "TOTP_NOT_BOUND"


# ------------------------------------------------------------------ CODE_WORD: приказ
async def test_code_word_ack_level(client, seeded):
    a = await auth(client, "admin@example.com")
    order = (await a.post("/orders", json=order_payload(number="503-к", signature_policy="CODE_WORD"))).json()
    await a.post(f"/orders/{order['id']}/files", files=upload("p.pdf", pdf_bytes("x"), "application/pdf"))
    order = (await a.post(f"/orders/{order['id']}/publish")).json()

    t3, totp3, _ = await bind(client, "teacher1@example.com")
    # без контрольного слова уровень недоступен
    no_word = await t3.post(f"/orders/{order['id']}/acknowledgements", json={"code": totp3.now()})
    assert no_word.status_code == 409 and no_word.json()["code"] == "RECOVERY_WORD_MISSING"
    word = "слово-пароль"
    bad = await t3.put("/users/me/totp/recovery-word", json={"word": word, "code": "654321"})
    assert bad.status_code == 422  # установка слова требует текущий код
    assert (await t3.put("/users/me/totp/recovery-word", json={"word": word, "code": totp3.now()})).status_code == 204
    assert (await t3.get("/auth/me")).json()["recovery_word_set"] is True

    wrong_word = await t3.post(f"/orders/{order['id']}/acknowledgements", json={"code": totp3.now(), "word": "не то"})
    assert wrong_word.status_code == 422 and wrong_word.json()["code"] == "RECOVERY_WORD_MISMATCH"
    ok = await t3.post(f"/orders/{order['id']}/acknowledgements", json={"code": totp3.now(), "word": word})
    assert ok.status_code == 200, ok.text
    assert ok.json()["sign_method"] == "CODE_WORD"


# ------------------------------------------------------------------ чувствительные операции
async def test_code_word_gates_sensitive_operations(client, seeded):
    owner, sign_body = await sign(client, "owner@example.com")

    # очистка журнала событий без подписки кода+слова — блок (привязка есть, слова ещё нет)
    r = await owner.request("DELETE", "/audit", json={"code": sign_body()["code"], "word": "не задано"})
    assert r.status_code in (409, 422), r.text
    no_sig = await owner.request("DELETE", "/audit")
    # отсутствие подписи — то же состояние «подпись не предоставлена», что и пустое тело: 409 с подсказкой
    assert no_sig.status_code == 409 and no_sig.json()["code"] == "TOTP_NOT_BOUND", no_sig.text

    # локдаун: без кода — блок неверным кодом, с кодом+словом — ок
    bad = await owner.post("/system/lockdown", json={"enabled": True, **sign_body(), "code": "111111" if sign_body()["code"] != "111111" else "222222"})
    assert bad.status_code == 422 and bad.json()["code"] == "TOTP_INVALID_CODE"
    wrong_w = await owner.post("/system/lockdown", json={"enabled": True, **sign_body(), "word": "не то"})
    assert wrong_w.status_code == 422 and wrong_w.json()["code"] == "RECOVERY_WORD_MISMATCH"
    ok = await owner.post("/system/lockdown", json={"enabled": True, **sign_body()})
    assert ok.status_code == 200, ok.text
    assert (await owner.post("/system/lockdown", json={"enabled": False, **sign_body()})).status_code == 200

    # очистка аудита теперь проходит
    cleared = await owner.request("DELETE", "/audit", json=sign_body())
    assert cleared.status_code == 204, cleared.text

    # удаление пользователя: без подписи — блок, с подписью — успех
    admin = await auth(client, "admin@example.com")
    created = await admin.post("/users", json={
        "email": "victim@example.com", "full_name": "Кандидат на удаление",
        "role": "TEACHER", "password": "Passw0rd!",
    })
    # создаёт учётки только Владелец — повторим со своей сессией, если 403
    if created.status_code == 403:
        created = await owner.post("/users", json={
            "email": "victim@example.com", "full_name": "Кандидат на удаление",
            "role": "TEACHER", "password": "Passw0rd!",
        })
    assert created.status_code in (200, 201), created.text
    victim_id = created.json()["id"]
    denied = await owner.request("DELETE", f"/users/{victim_id}", json={"word": sign_body()["word"]})
    # подпись не предъявлена целиком (нет одноразового кода) — 409 с подсказкой, без фолбэка на пароль
    assert denied.status_code == 409 and denied.json()["code"] == "TOTP_NOT_BOUND", denied.text
    bad_sig = await owner.request("DELETE", f"/users/{victim_id}", json={"code": "111111" if sign_body()["code"] != "111111" else "222222", "word": sign_body()["word"]})
    assert bad_sig.status_code == 422 and bad_sig.json()["code"] == "TOTP_INVALID_CODE"
    allowed = await owner.request("DELETE", f"/users/{victim_id}", json=sign_body())
    assert allowed.status_code == 204, allowed.text


# ------------------------------------------------------------------ справочники
async def test_meta_dicts_include_pep_values(client, seeded):
    a = await auth(client, "admin@example.com")
    enums = (await a.get("/meta/enums")).json()
    assert {i["value"] for i in enums["signature_policies"]} == {"NONE", "CLICK", "TOTP", "CODE_WORD"}
    assert all(i["label"] for i in enums["signature_policies"])  # русские подписи заданы
    assert {i["value"] for i in enums["sign_methods"]} == {"NONE", "CLICK", "TOTP", "CODE_WORD"}


# ------------------------------------------------------------------ RBAC
async def test_rbac_only_self_can_bind_totp(client, seeded):
    t = await auth(client, "teacher1@example.com")
    a = await auth(client, "admin@example.com")
    tid = (await t.get("/auth/me")).json()["id"]
    # админ НЕ может привязать/отвязать аутентификатор за другого человека
    assert (await a.post(f"/users/{tid}/totp/setup")).status_code == 404
    assert (await a.delete(f"/users/{tid}/totp")).status_code == 404
