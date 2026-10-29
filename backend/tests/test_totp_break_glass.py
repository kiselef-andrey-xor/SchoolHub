"""Резервный (аварийный) сброс аутентификатора Владельца по контрольному слову (v1.12).

Закрывает единственный не покрытый штатным механизмом случай: телефон потерял
САМ Владелец. Обычный сброс (`POST /users/{id}/totp-reset`) выполняет Владелец и
подписывает его кодом TOTP + словом — подписать нечем, если устройство утеряно.
Восстановление из копии и выход из локдауна тоже требуют код, то есть система
блокировала сама себя.

Проверяем:
  * успешный сброс: пароль + заранее заданное контрольное слово → привязка снята,
    ВСЕ сессии Владельца отозваны (включая текущую), cookie сброшен, аудит и
    уведомление администрации на месте, а вход снова работает;
  * слово сравнивается без учёта регистра, но неверный пароль или слово — 422;
  * каждая неудача остаётся в журнале ОТДЕЛЬНОЙ транзакцией (основная откатилась);
  * подбор троттлится: после исчерпания лимита — 429, причём отдельным ключом от
    подписного лимитера (успешная проверка кода его не обнуляет);
  * доступно ТОЛЬКО Владельцу: сотрудник получает 403 с подсказкой, что его
    сбрасывает Владелец;
  * без заданного контрольного слова — 409 RECOVERY_WORD_MISSING (сброс
    невозможен, и это надо было настроить заранее);
  * доменная учётка Владельца аварийным сбросом не обслуживается — 409.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import sessions as session_service
from app.services import totp
from tests.conftest import DEMO_PASSWORD, auth, new_client
from tests.test_totp_pep import bind, sign

pytestmark = pytest.mark.asyncio

WORD = "синий-трактор-42"
BREAK_GLASS = "/users/me/totp/break-glass"


async def prepare_owner(client, word: str | None = WORD):
    """Привязывает аутентификатор Владельца и (опционально) задаёт контрольное слово."""
    cli, otp, secret = await bind(client, "owner@example.com")
    if word:
        resp = await cli.put("/users/me/totp/recovery-word",
                             json={"code": otp.now(), "word": word})
        assert resp.status_code == 204, resp.text
    me = (await cli.get("/auth/me")).json()
    assert me["totp_enabled"] is True
    assert me["recovery_word_set"] is bool(word)
    return cli, otp, secret


# ----------------------------------------------------------------------- успех
async def test_owner_resets_own_binding_by_password_and_word(client, seeded):
    """Основной сценарий: телефон потерян — привязка снимается паролем и словом."""
    cli, otp, _ = await prepare_owner(client)

    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})
    assert resp.status_code == 204, resp.text
    # cookie снят: скомпрометированная вкладка теряет доступ вместе с подписью
    assert resp.headers.get("set-cookie", "").startswith(f"{session_service.COOKIE_NAME}=")

    me = (await cli.get("/auth/me")).json()
    assert me["totp_enabled"] is False, "привязка аутентификатора снята"
    assert me["recovery_word_set"] is False, "контрольное слово уничтожено вместе с секретом"

    # штатный сброс чужой привязки снова требует код Владельца — фолбэка на пароль нет
    reset = await cli.post(f"/users/{seeded['teacher']}/totp-reset", json={})
    assert reset.status_code == 409, reset.text
    assert "не привязан" in reset.json()["detail"]

    # прежняя сессия отозвана: refresh-токен больше не работает
    fresh = new_client(client)
    refreshed = await fresh.post("/auth/refresh")
    assert refreshed.status_code == 401, "все сессии Владельца должны быть отозваны"

    # вход по паролю снова доступен, а аутентификатор можно привязать заново
    again = await auth(client, "owner@example.com")
    assert (await again.get("/auth/me")).json()["totp_enabled"] is False
    setup = await again.post("/users/me/totp/setup")
    assert setup.status_code == 200, setup.text
    assert setup.json()["secret"], "новый секрет выдаётся одноразово"


async def test_word_is_case_insensitive_and_surrounding_spaces_are_ignored(client, seeded):
    """Слово вводит человек: регистр и случайные пробелы не должны ломать восстановление."""
    cli, _, _ = await prepare_owner(client)
    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": f"  {WORD.upper()}  "})
    assert resp.status_code == 204, resp.text


async def test_reset_is_written_to_audit_and_admins_are_notified(client, seeded):
    """Снятие подписи с Владельца видно в журнале и администрации."""
    cli, _, _ = await prepare_owner(client)
    assert (await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})).status_code == 204

    admin = await auth(client, "admin@example.com")
    notes = (await admin.get("/notifications", params={"unread_only": "false", "per_page": 100})).json()["items"]
    alerts = [n for n in notes if n["type"] == "TOTP_BREAK_GLASS"]
    assert len(alerts) == 1, "администрация должна получить уведомление об аварийном сбросе"
    assert "Аварийный сброс аутентификатора Владельца" in alerts[0]["title"]
    assert "смените пароль Владельца" in alerts[0]["body"], "в уведомлении нужен план действий"

    journal = (await admin.get("/audit", params={"action": "TOTP_BREAK_GLASS", "per_page": 50})).json()["items"]
    assert journal, "событие аварийного сброса обязано быть в журнале"
    assert journal[0]["entity_type"] == "user" and journal[0]["entity_id"] == seeded["owner"]
    assert journal[0]["summary"] == "Аварийный сброс аутентификатора Владельца (по контрольному слову)"


# ----------------------------------------------------------------------- отказы
async def test_wrong_password_or_word_is_rejected_and_logged(client, seeded):
    """Неверный пароль и неверное слово отклоняются; обе попытки попадают в журнал."""
    cli, _, _ = await prepare_owner(client)

    bad_password = await cli.post(BREAK_GLASS, json={"password": "wrong-password", "word": WORD})
    assert bad_password.status_code == 422 and bad_password.json()["code"] == "BREAK_GLASS_FAILED"
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True, "привязка осталась на месте"

    bad_word = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": "красный-трактор"})
    assert bad_word.status_code == 422 and bad_word.json()["code"] == "BREAK_GLASS_FAILED"

    admin = await auth(client, "admin@example.com")
    rows = (await admin.get("/audit", params={"action": "SIGNATURE_FAILED", "per_page": 100})).json()["items"]
    details = [r["changes"].get("reason") for r in rows]
    # записи сделаны ОТДЕЛЬНОЙ транзакцией: запрос завершился 422, основная откатилась,
    # а след подбора обязан остаться — иначе расследование невозможно
    assert "BREAK_GLASS_PASSWORD" in details, details
    assert "BREAK_GLASS_WORD" in details, details


async def test_brute_force_is_throttled_with_a_dedicated_key(client, seeded):
    """Подбор слова ограничен отдельным лимитером (5 попыток / 30 минут)."""
    cli, _, _ = await prepare_owner(client)

    limit = settings.break_glass_max_attempts
    for attempt in range(limit):
        resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": f"неверное-{attempt}"})
        assert resp.status_code == 422, (attempt, resp.status_code)
    blocked = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})
    assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED"
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True, "привязка не снята под давлением подбора"

    # отдельный ключ от подписного лимитера: успешная проверка кода TOTP
    # (register_success по своему ключу) не обнуляет счётчик подбора слова
    from app.services.rate_limit import signature_limiter

    signature_limiter.register_success(None, str(seeded["owner"]))
    still_blocked = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})
    assert still_blocked.status_code == 429


async def test_available_only_to_owner(client, seeded):
    """Сотруднику и администрации аварийный сброс недоступен: их привязку снимает Владелец.

    Контрольное слово задаём заранее (sign()), чтобы отказ был именно ПО РОЛИ,
    а не из-за отсутствия слова.
    """
    cli, sig = await sign(client, "teacher1@example.com", word=WORD)
    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": sig()["word"]})
    assert resp.status_code == 403, resp.text
    assert "только Владельцу" in resp.json()["detail"]
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True

    admin_cli, admin_sig = await sign(client, "admin@example.com", word=WORD)
    resp = await admin_cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": admin_sig()["word"]})
    assert resp.status_code == 403, resp.text

    # штатный путь для сотрудника — сброс Владельцем, подписанный «код + слово»
    owner, owner_sig = await sign(client, "owner@example.com", word="владелец-слово")
    reset = await owner.post(f"/users/{seeded['teacher']}/totp-reset", json=owner_sig())
    assert reset.status_code == 204, reset.text
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is False


async def test_without_recovery_word_the_reset_is_impossible(client, seeded):
    """Слово не задано — сброс запрещён с подсказкой, а не «молча не работает»."""
    cli, _, _ = await prepare_owner(client, word=None)
    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": "любое-слово"})
    assert resp.status_code == 409 and resp.json()["code"] == "RECOVERY_WORD_MISSING"
    assert "задайте его заранее" in resp.json()["detail"].lower()
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True


async def test_without_binding_there_is_nothing_to_reset(client, seeded):
    cli = await auth(client, "owner@example.com")
    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})
    assert resp.status_code == 409 and "не привязан" in resp.json()["detail"]


async def test_ldap_owner_is_not_served_by_break_glass(client, seeded, session_factory):
    """Доменный Владелец: пароль проверяет AD, поэтому аварийный сброс запрещён.

    Принимать доменный пароль в запросе к SchoolHub означало бы завести второй,
    никем не контролируемый канал проверки учётных данных каталога.
    """
    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.user import User

        owner = (await session.execute(select(User).where(User.email == "owner@example.com"))).scalar_one()
        owner.auth_source = "ldap"
        await session.commit()

    cli, _, _ = await prepare_owner(client)
    resp = await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD, "word": WORD})
    assert resp.status_code == 409, resp.text
    assert "доменная" in resp.json()["detail"]
    assert (await cli.get("/auth/me")).json()["totp_enabled"] is True


async def test_missing_fields_are_validation_errors(client, seeded):
    cli, _, _ = await prepare_owner(client)
    assert (await cli.post(BREAK_GLASS, json={"password": DEMO_PASSWORD})).status_code == 422
    assert (await cli.post(BREAK_GLASS, json={"word": WORD})).status_code == 422
    assert (await cli.post(BREAK_GLASS, json={})).status_code == 422
    # анонимный запрос не проходит аутентификацию
    anon = new_client(client)
    assert (await anon.post(BREAK_GLASS, json={"password": "x", "word": "y"})).status_code == 401


async def test_service_rejects_non_owner_directly(client, seeded, session_factory):
    """Защита на уровне сервиса: даже вызов мимо API не снимет чужую привязку."""
    async with session_factory() as session:
        from sqlalchemy import select

        from app.enums import Role
        from app.errors import AppError
        from app.models.user import User

        teacher = (await session.execute(
            select(User).where(User.email == "teacher1@example.com")
        )).scalar_one()
        teacher.totp_secret_enc = totp.seal("JBSWY3DPEHPK3PXP", "слово")
        teacher.totp_enabled = True
        await session.commit()

        with pytest.raises(AppError) as exc:
            await totp.break_glass_reset(session, teacher, DEMO_PASSWORD, "слово")
        assert exc.value.status_code == 403
        await session.rollback()

        admin = (await session.execute(select(User).where(User.role == Role.ADMIN.value))).scalar_one()
        admin.totp_secret_enc = totp.seal("JBSWY3DPEHPK3PXP", "слово")
        admin.totp_enabled = True
        await session.commit()
        with pytest.raises(AppError) as exc:
            await totp.break_glass_reset(session, admin, DEMO_PASSWORD, "слово")
        assert exc.value.status_code == 403
