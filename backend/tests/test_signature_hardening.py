"""Защита электронной подписи (ПЭП) от подбора и от «снятия чужой подписи».

Три сценария, которые раньше были возможны:

1. **Перебор одноразового кода.** Код TOTP — 6 цифр, а с окном ±1 шаг одновременно
   действительны три значения. Держатель чужого access-токена (XSS, украденный ноутбук)
   мог бы подбирать код запросами и в итоге подписать приказ/ознакомление от имени
   сотрудника. Теперь неудачные проверки троттлятся (429 после `TOTP_MAX_ATTEMPTS`
   за `TOTP_LOCK_WINDOW_MIN` минут) и каждая неудача пишется в журнал событий.

2. **Молчаливая отвязка чужого аутентификатора.** `DELETE /users/me/totp` не требовал
   никакого подтверждения: снял привязку → привязал свой аутентификатор → подписываешь
   от имени сотрудника. Теперь действующая привязка снимается только текущим кодом.

3. **Потерянный телефон.** Раньше сотрудник мог отвязать аутентификатор без кода —
   это и был аварийный путь, но он же был дырой. Теперь есть отдельный: Владелец
   сбрасывает привязку в карточке сотрудника, подписав операцию «код + слово»,
   а сотрудник получает уведомление, и действие попадает в журнал.
"""
from __future__ import annotations

import pyotp
import pytest

from tests.conftest import auth
from tests.test_totp_pep import bind, sign

pytestmark = pytest.mark.asyncio


def _wrong_code(totp_obj) -> str:
    """Код, который заведомо не подходит (не совпадает с текущим)."""
    now = totp_obj.now()
    return next(c for c in ("000000", "111111", "222222", "333333") if c != now)


async def test_wrong_signature_codes_are_throttled_and_audited(client, seeded):
    """Перебор кода упирается в 429, а все неудачи видны Владельцу в журнале."""
    cli, totp_obj, _ = await bind(client, "teacher1@example.com")

    wrong = _wrong_code(totp_obj)
    statuses = []
    for _ in range(7):
        resp = await cli.put(
            "/users/me/totp/recovery-word", json={"word": "слово-перебор", "code": wrong}
        )
        statuses.append(resp.status_code)

    # первые попытки — «неверный код» (422), дальше лимит (429)
    assert statuses[0] == 422, statuses
    assert 429 in statuses, statuses
    assert statuses[-1] == 429, statuses
    blocked = next(s for s in statuses if s == 429)
    assert blocked == 429

    # контрольное слово НЕ установлено: перебор не должен ничего менять
    me = (await cli.get("/auth/me")).json()
    assert me["recovery_word_set"] is False

    owner = await auth(client, "owner@example.com")
    logs = (await owner.get("/audit", params={"action": "SIGNATURE_FAILED", "per_page": 50})).json()["items"]
    reasons = [(item.get("changes") or {}).get("reason") for item in logs]
    assert "TOTP_INVALID_CODE" in reasons, reasons
    assert "RATE_LIMITED" in reasons, reasons
    # журнал указывает, чья именно подпись подбиралась
    assert any(item.get("entity_id") == me["id"] for item in logs), logs


async def test_correct_code_resets_the_throttle(client, seeded):
    """Успешная подпись снимает блокировку: честный пользователь не страдает."""
    cli, totp_obj, _ = await bind(client, "teacher2@example.com")
    wrong = _wrong_code(totp_obj)
    for _ in range(3):
        assert (await cli.put(
            "/users/me/totp/recovery-word", json={"word": "слово-перебор", "code": wrong}
        )).status_code == 422

    ok = await cli.put(
        "/users/me/totp/recovery-word", json={"word": "синий-трактор", "code": totp_obj.now()}
    )
    assert ok.status_code == 204, ok.text
    assert (await cli.get("/auth/me")).json()["recovery_word_set"] is True

    # счётчик обнулён: снова доступны попытки (до лимита)
    for _ in range(3):
        assert (await cli.put(
            "/users/me/totp/recovery-word", json={"word": "другое-слово", "code": wrong}
        )).status_code == 422


async def test_recovery_word_mismatch_is_throttled_too(client, seeded):
    """Контрольное слово — второй фактор чувствительных операций, его подбор тоже троттлится.

    Проверяем на локдауне (Владелец, «код + слово»): код верный, слово чужое.
    """
    from app.services import lockdown

    owner, current = await sign(client, "owner@example.com", word="красный-слон")
    statuses = []
    for _ in range(7):
        body = {"enabled": True, "code": current()["code"], "word": "не-то-слово"}
        resp = await owner.post("/system/lockdown", json=body)
        statuses.append(resp.status_code)
        if resp.status_code == 422:
            assert resp.json()["code"] == "RECOVERY_WORD_MISMATCH", resp.text

    assert statuses[0] == 422, statuses
    assert 429 in statuses, statuses
    # локдаун так и не включился: перебор слова ничего не меняет
    assert (await owner.get("/system/lockdown")).json()["enabled"] is False
    lockdown.invalidate_cache()


async def test_owner_resets_lost_phone(client, seeded):
    """Аварийный путь: Владелец сбрасывает аутентификатор, подписав операцию ПЭП."""
    teacher, totp_obj, _ = await bind(client, "teacher1@example.com")
    teacher_id = (await teacher.get("/auth/me")).json()["id"]
    owner, current = await sign(client, "owner@example.com")
    admin = await auth(client, "admin@example.com")

    # администрация сбрасывать чужую подпись не может
    assert (await admin.post(f"/users/{teacher_id}/totp-reset", json=current())).status_code == 403

    # Владелец без подписи — блокировка с подсказкой
    no_sig = await owner.post(f"/users/{teacher_id}/totp-reset", json={})
    assert no_sig.status_code == 409, no_sig.text
    assert no_sig.json()["code"] in {"TOTP_NOT_BOUND", "RECOVERY_WORD_MISSING"}

    # неверный код — не проходит
    bad = await owner.post(
        f"/users/{teacher_id}/totp-reset",
        json={"code": _wrong_code(pyotp.TOTP("JBSWY3DPEHPK3PXP")), "word": current()["word"]},
    )
    assert bad.status_code == 422, bad.text

    # подпись «код + слово» — сброс выполняется
    ok = await owner.post(f"/users/{teacher_id}/totp-reset", json=current())
    assert ok.status_code == 204, ok.text

    me = (await teacher.get("/auth/me")).json()
    assert me["totp_enabled"] is False
    assert me["recovery_word_set"] is False

    # сотрудник уведомлён (иначе он не поймёт, почему подпись перестала работать)
    notes = (await teacher.get("/notifications")).json()["items"]
    assert any(n["type"] == "TOTP_RESET" for n in notes), notes

    # действие Владельца видно в журнале
    logs = (await owner.get("/audit", params={"action": "TOTP_RESET", "per_page": 20})).json()["items"]
    assert logs and logs[0]["entity_id"] == teacher_id, logs

    # повторный сброс — нечего сбрасывать
    again = await owner.post(f"/users/{teacher_id}/totp-reset", json=current())
    assert again.status_code == 409, again.text

    # после сброса сотрудник привязывает аутентификатор заново (без кода — привязки нет)
    assert (await teacher.post("/users/me/totp/setup")).status_code == 200
    assert (await teacher.request("DELETE", "/users/me/totp")).status_code == 204
