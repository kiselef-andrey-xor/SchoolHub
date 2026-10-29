"""Электронная подпись через аутентификатор (TOTP, RFC 6238) + политика подписи.

Ответственности:
  * привязка: секрет выдаётся ОДНОРАЗОВО (никогда больше не возвращается API),
    подтверждение первым кодом — до подтверждения привязка не считается действующей;
  * отвязка: сброс секрета/флагов;
  * проверка одноразового кода (±1 окно) и уровня «код + контрольное слово»;
  * единая точка проверки чувствительных операций: восстановление из копии,
    очистка журнала событий, локдаун, удаление пользователя — требуют «код + слово»;
  * хранение секрета — Fernet-шифрование (ключ выводится из SECRET_KEY), в БД лежит
    только totp_secret_enc = token(secret|слово).

Без фолбэков: если привязки нет, а операция/документ требует код — запрос блокируется
с подсказкой (409), пароль при этом НЕ запрашивается и НЕ проверяется.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timezone

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import SignaturePolicy
from app.errors import AppError, bad_request, conflict, forbidden
from app.models.user import User
from app.services.rate_limit import signature_limiter

logger = logging.getLogger("schoolhub.totp")


async def _audit_failure(user: User, reason: str, *, extra: dict | None = None) -> None:
    """Неудачная попытка подписи — в журнал ОТДЕЛЬНОЙ транзакцией.

    Именно отдельной: запрос завершится ошибкой 4xx/429, основная транзакция
    откатится, и запись в журнале пропала бы. А расследовать нужно как раз
    неудачные попытки (перебор кода чужим токеном).
    """
    from app.services.audit import write_audit_detached

    changes = {"reason": reason}
    if extra:
        changes.update(extra)
    await write_audit_detached(
        actor=user, action="SIGNATURE_FAILED", entity_type="user", entity_id=user.id, changes=changes
    )


def _fernet() -> Fernet:
    """Ключ шифрования секретов TOTP — детерминированно выводится из SECRET_KEY."""
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# --------------------------------------------------------------- хранение
def seal(secret_b32: str, word: str | None) -> str:
    payload = secret_b32 if not word else f"{secret_b32}|{word.strip()}"
    return _fernet().encrypt(payload.encode("utf-8")).decode("ascii")


def unseal(user: User) -> tuple[str, str | None]:
    """Возвращает (секрет, контрольное слово | None); (None, None) — привязки нет."""
    if not user.totp_secret_enc:
        return None, None
    try:
        payload = _fernet().decrypt(user.totp_secret_enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise AppError(500, "Не удалось расшифровать секрет TOTP (проверьте SECRET_KEY)", code="TOTP_STORAGE_ERROR")
    if "|" in payload:
        secret, _, word = payload.partition("|")
        return secret, word or None
    return payload, None


# ------------------------------------------------------------- привязка
async def setup_totp(session: AsyncSession, user: User) -> dict:
    """Начинает привязку: генерирует новый секрет (старый аннулируется).

    Секрет возвращается ОДИН РАЗ — для ввода в приложение-аутентификатор / QR.
    Пока confirm_totp не подтверждён первым кодом, totp_enabled остаётся False.
    """
    if user.totp_secret_enc and user.totp_confirmed_at is not None:
        raise conflict("Аутентификатор уже привязан. Сначала выполните отвязку")
    secret = pyotp.random_base32()
    user.totp_secret_enc = seal(secret, None)
    user.totp_enabled = False
    user.totp_confirmed_at = None
    await session.flush()
    label = (user.login or user.email or str(user.id)).strip()
    uri = pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name="SchoolHub")
    return {
        "secret": secret,          # одноразово: только этот ответ
        "otpauth_uri": uri,        # для QR-кода в приложении-аутентификаторе
        "algorithm": "SHA1",
        "digits": 6,
        "period": 30,
    }


async def confirm_totp(session: AsyncSession, user: User, code: str) -> User:
    """Подтверждение привязки ПЕРВЫМ кодом из приложения."""
    secret, _ = unseal(user)
    if not secret:
        raise bad_request("Привязка не начата: сначала запросите секрет (POST /users/me/totp/setup)")
    if user.totp_confirmed_at is not None:
        raise conflict("Аутентификатор уже привязан")
    if not verify_code(secret, code):
        raise AppError(422, "Код не подошёл. Проверьте время на устройстве и повторите", code="TOTP_INVALID_CODE")
    user.totp_enabled = True
    user.totp_confirmed_at = datetime.now(timezone.utc)
    await session.flush()
    return user


async def remove_totp(session: AsyncSession, user: User) -> None:
    """Отвязка аутентификатора: секрет уничтожается, флаги сбрасываются."""
    user.totp_secret_enc = None
    user.totp_enabled = False
    user.totp_confirmed_at = None
    user.recovery_word_set_at = None
    await session.flush()


# --------------------------------------------------------- аварийный сброс
def _break_glass_key(user: User) -> str:
    """Ключ троттлинга аварийного сброса — отдельный от ключей кода и слова."""
    return f"{user.id}:break-glass"


async def break_glass_reset(session: AsyncSession, user: User, password: str, word: str,
                            *, ip: str | None = None) -> None:
    """Резервный сброс привязки аутентификатора по ПАРОЛЮ + КОНТРОЛЬНОМУ СЛОВУ (v1.12).

    Зачем это нужно
    ---------------
    Штатный сброс (`POST /users/{id}/totp-reset`) выполняет Владелец и подписывает
    его кодом TOTP + контрольным словом. Но если телефон потерял САМ Владелец,
    подписать ему нечего: сбросить некому, а восстановление из копии и выход из
    локдауна тоже требуют код. Возникает тупик, из которого без этой процедуры
    выводил только прямой доступ к базе на NAS.

    Как защищено
    ------------
    Процедура снимает электронную подпись с самой привилегированной учётной записи,
    поэтому требует двух независимых факторов и узкого троттлинга:

      1. **пароль** — проверяется локальным хешем (bcrypt); доменная учётка
         Владельца аварийным сбросом не обслуживается (её пароль живёт в AD,
         а привязка TOTP — в SchoolHub, и проверять их в одном запросе нельзя);
      2. **контрольное слово**, заданное заранее (`PUT /users/me/totp/recovery-word`)
         и хранящееся зашифрованным вместе с секретом. Слово сравнивается
         `secrets.compare_digest` без учёта регистра;
      3. **троттлинг** `break_glass_limiter` — 5 попыток на 30 минут, отдельный
         ключ от подписного лимитера (иначе успешная проверка кода обнуляла бы
         счётчик подбора слова);
      4. каждая неудача пишется в журнал ОТДЕЛЬНОЙ транзакцией (`SIGNATURE_FAILED`) —
         запрос завершится 4xx, основная транзакция откатится, а след подбора
         обязан остаться;
      5. после успеха отзываются ВСЕ refresh-сессии Владельца, включая текущую:
         скомпрометированная вкладка теряет доступ вместе со сброшенной подписью,
         а сам сброс невозможно «доделать» чужим токеном;
      6. уведомление уходит Владельцу и всей администрации — снятие подписи с
         Владельца должно быть видно не только ему.

    Контрольное слово уничтожается вместе с секретом: после сброса его нельзя
    предъявить повторно, а новую привязку Владелец делает уже с новым словом.
    """
    from app.enums import Role
    from app.models.user import UNUSABLE_PASSWORD
    from app.services import audit, notifications, sessions
    from app.services.rate_limit import break_glass_limiter

    if user.role != Role.OWNER:
        raise forbidden(
            "Аварийный сброс по контрольному слову доступен только Владельцу системы. "
            "Сотруднику привязку сбрасывает Владелец в его карточке "
            "(Пользователи → «Сбросить аутентификатор»)"
        )
    if user.auth_source != "local" or user.password_hash in (UNUSABLE_PASSWORD, "", None):
        raise conflict(
            "Учётная запись Владельца доменная (Active Directory): пароль проверяет контроллер, "
            "а привязка аутентификатора хранится в SchoolHub. Аварийный сброс для неё недоступен — "
            "воспользуйтесь локальной учёткой Владельца (break-glass) или восстановлением из копии"
        )
    if not user.totp_secret_enc:
        raise conflict("Аутентификатор не привязан — сбрасывать нечего")

    break_glass_limiter.check(ip, _break_glass_key(user))

    from app.security import verify_password_async

    if not await verify_password_async(password or "", user.password_hash):
        break_glass_limiter.register_failure(ip, _break_glass_key(user))
        logger.warning("Аварийный сброс: неверный пароль Владельца (%s)", user.email)
        await _audit_failure(user, "BREAK_GLASS_PASSWORD", extra={"factor": "password"})
        raise AppError(422, "Пароль указан неверно", code="BREAK_GLASS_FAILED")

    _, stored_word = unseal(user)
    if not stored_word:
        break_glass_limiter.register_failure(ip, _break_glass_key(user))
        await _audit_failure(user, "BREAK_GLASS_NO_WORD")
        raise AppError(
            409,
            "Контрольное слово не задано — аварийный сброс невозможен. "
            "Задайте его заранее: Профиль → «Электронная подпись» → «Контрольное слово»",
            code="RECOVERY_WORD_MISSING",
        )
    if not secrets_equal(word or "", stored_word):
        break_glass_limiter.register_failure(ip, _break_glass_key(user))
        logger.warning("Аварийный сброс: контрольное слово не совпало (%s)", user.email)
        await _audit_failure(user, "BREAK_GLASS_WORD", extra={"factor": "word"})
        raise AppError(422, "Контрольное слово не совпадает", code="BREAK_GLASS_FAILED")

    break_glass_limiter.register_success(ip, _break_glass_key(user))
    await remove_totp(session, user)
    # все сессии Владельца отзываются: сброс подписи обязан означать и конец сессий
    await sessions.revoke_all_for_user(session, user.id, "totp_break_glass")

    await audit.write_audit(
        session, actor=user, action="TOTP_BREAK_GLASS", entity_type="user", entity_id=user.id,
        changes={"full_name": user.full_name, "email": user.email,
                 "reason": "потеря устройства с аутентификатором"},
    )

    admins = (await session.execute(
        select(User.id).where(User.is_active.is_(True), User.is_deleted.is_(False),
                              User.role.in_([Role.OWNER.value, Role.ADMIN.value]))
    )).scalars().all()
    await notifications.push_many(
        session, user_ids=list(admins), type_=notifications.TOTP_BREAK_GLASS,
        title="Аварийный сброс аутентификатора Владельца",
        body="Привязка приложения-аутентификатора к учётной записи Владельца снята по паролю "
             "и контрольному слову (утеря устройства). Электронная подпись кодом недоступна, "
             "пока Владелец не привяжет аутентификатор заново. Все его сессии завершены. "
             "Если это сделали не по его инициативе — немедленно смените пароль Владельца "
             "и проверьте журнал событий.",
        entity_type="user", entity_id=user.id,
    )


# ------------------------------------------------------------ проверка кодов
def verify_code(secret_b32: str, code: str | None) -> bool:
    """Одноразовый код с окном ±1step (допуск часовых расхождений приложений)."""
    digits = (code or "").strip()
    if not digits.isdigit():
        return False
    return pyotp.TOTP(secret_b32).verify(digits, valid_window=1)


async def check_totp_code(user: User, code: str | None, *, require_present: bool = True) -> None:
    """Проверяет одноразовый код пользователя. Без привязки — блокировка с подсказкой.

    require_present=True (чувствительные операции): отсутствие кода в запросе —
    то же состояние «подпись не предоставлена», что и отсутствие привязки: 409
    TOTP_NOT_BOUND с подсказкой, без фолбэка на пароль. Неверный/просроченный
    код остаётся 422 TOTP_INVALID_CODE.
    """
    if not (user.totp_enabled and user.totp_confirmed_at is not None):
        raise AppError(
            409,
            "Требуется электронная подпись кодом из аутентификатора, но он не привязан. "
            "Привяжите аутентификатор в профиле (раздел «Электронная подпись»)",
            code="TOTP_NOT_BOUND",
        )
    # Перебор кода: после TOTP_MAX_ATTEMPTS неудач проверка блокируется на
    # TOTP_LOCK_WINDOW_MIN минут (429). Проверяем ДО валидации, чтобы сам факт
    # исчерпания лимита тоже попал в журнал.
    try:
        signature_limiter.check(None, str(user.id))
    except AppError:
        logger.warning("Подпись заблокирована троттлингом: user=%s", user.email)
        await _audit_failure(user, "RATE_LIMITED")
        raise
    if require_present and not (code or "").strip():
        raise AppError(
            409,
            "Операция требует электронную подпись: приложите одноразовый код из "
            "аутентификатора (и контрольное слово, если требуется уровень «код + слово»)",
            code="TOTP_NOT_BOUND",
        )
    secret, _ = unseal(user)
    if not secret:
        raise AppError(409, "Привязка аутентификатора повреждена — перевяжите её в профиле", code="TOTP_NOT_BOUND")
    if not verify_code(secret, code):
        signature_limiter.register_failure(None, str(user.id))
        logger.warning("Неверный код аутентификатора: user=%s", user.email)
        await _audit_failure(user, "TOTP_INVALID_CODE")
        raise AppError(422, "Неверный или просроченный код аутентификатора", code="TOTP_INVALID_CODE")
    signature_limiter.register_success(None, str(user.id))


async def set_recovery_word(session: AsyncSession, user: User, word: str, code: str | None) -> User:
    """Устанавливает/заменяет контрольное слово (требует текущий код TOTP).

    Слово хранится вместе с секретом в зашифрованном поле; повторная установка
    возможна только с действующим кодом — защита от подмены слова без подписи.
    """
    w = (word or "").strip()
    if len(w) < 4:
        raise bad_request("Контрольное слово — минимум 4 символа")
    # при установке слова код обязателен, но пустой код — ошибка формата (422), а не 409
    await check_totp_code(user, code, require_present=False)
    if not (code or "").strip():
        raise AppError(422, "Требуется одноразовый код аутентификатора", code="TOTP_INVALID_CODE")
    secret, _ = unseal(user)
    user.totp_secret_enc = seal(secret, w)
    user.recovery_word_set_at = datetime.now(timezone.utc)
    await session.flush()
    return user


def _word_key(user: User) -> str:
    """Ключ троттлинга контрольного слова — ОТДЕЛЬНЫЙ от ключа кода.

    Иначе успешная проверка кода (`register_success`) обнуляла бы счётчик неудач
    слова на каждом запросе, и подбор слова не ограничивался бы вовсе.
    """
    return f"{user.id}:word"


async def require_code_word(user: User, code: str | None, word: str | None) -> None:
    """Максимальный уровень подписи: одноразовый код + контрольное слово.

    Применяется к чувствительным операциям: восстановление из копии, очистка
    журнала событий, включение/выключение локдауна, удаление пользователя,
    сброс чужого аутентификатора.
    """
    await check_totp_code(user, code)
    try:
        signature_limiter.check(None, _word_key(user))
    except AppError:
        logger.warning("Подбор контрольного слова заблокирован: user=%s", user.email)
        await _audit_failure(user, "RATE_LIMITED", extra={"factor": "word"})
        raise
    _, stored_word = unseal(user)
    if not stored_word:
        raise AppError(
            409,
            "Операция требует подпись «код + контрольное слово». Задайте контрольное слово "
            "в профиле (PUT /users/me/totp/recovery-word)",
            code="RECOVERY_WORD_MISSING",
        )
    supplied = (word or "").strip()
    if not supplied or not secrets_equal(supplied, stored_word):
        # слово — второй фактор самых чувствительных операций (восстановление из копии,
        # очистка журнала, удаление сотрудника): его подбор тоже троттлим и логируем
        signature_limiter.register_failure(None, _word_key(user))
        logger.warning("Контрольное слово не совпало: user=%s", user.email)
        await _audit_failure(user, "RECOVERY_WORD_MISMATCH")
        raise AppError(422, "Контрольное слово не совпадает", code="RECOVERY_WORD_MISMATCH")
    signature_limiter.register_success(None, _word_key(user))


def secrets_equal(a: str, b: str) -> bool:
    # compare_digest не принимает не-ASCII str (кириллица в слове — штатный случай),
    # поэтому сравниваем UTF-8 байты; регистронезависимо.
    return _secrets.compare_digest(a.strip().lower().encode("utf-8"), b.strip().lower().encode("utf-8"))


# --------------------------------------------------- политика подписи приказа
def policy_level(policy: str | None) -> int:
    order = [
        SignaturePolicy.NONE.value,
        SignaturePolicy.CLICK.value,
        SignaturePolicy.TOTP.value,
        SignaturePolicy.CODE_WORD.value,
    ]
    return order.index(policy) if policy in order else order.index(SignaturePolicy.CLICK.value)


def effective_policy(order, *, forced: str | None = None) -> str:
    """Действующая политика подписи приказа.

    Политика НЕ задана явно (NULL в БД и не передана при создании/изменении) —
    применяется «текущее» значение по умолчанию: клик «Ознакомлен». Явно
    выставленная политика всегда уважается (в т.ч. NONE для аудита).

    Приказы о движении обучающихся (тип STUDENTS) — ВСЕГДА усиленная подпись
    (не ниже TOTP), даже если в приказе выставлена более слабая политика.
    """
    from app.enums import ORDER_TYPE_SIGNATURE_POLICY

    declared = (
        forced
        if forced is not None
        else getattr(order, "signature_policy", None)
    ) or SignaturePolicy.CLICK.value
    auto = ORDER_TYPE_SIGNATURE_POLICY.get(str(getattr(order, "order_type", "") or ""))
    if auto and policy_level(auto) > policy_level(declared):
        return auto
    return declared


async def enforce_signature(session: AsyncSession, actor: User, policy: str, code: str | None, word: str | None):
    """Возвращает AckSignMethod по факту подписания; блокирует при нехватке уровня.

    Нет привязки, а документ требует код — блокируем с подсказкой (без фолбэка на пароль).
    """
    from app.enums import AckSignMethod

    level = policy_level(policy)
    if level <= policy_level(SignaturePolicy.NONE.value):
        return AckSignMethod.NONE
    if level == policy_level(SignaturePolicy.CLICK.value):
        return AckSignMethod.CLICK
    if level == policy_level(SignaturePolicy.TOTP.value):
        await check_totp_code(actor, code)
        return AckSignMethod.TOTP
    await require_code_word(actor, code, word)
    return AckSignMethod.CODE_WORD
