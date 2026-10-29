"""Ограничение частоты неудачных попыток: вход и электронная подпись (ПЭП).

Зачем нужно именно здесь:
  * **вход** — при аутентификации через домен каждая неудачная попытка идёт в Active
    Directory и приближает **блокировку доменной учётки** (lockout policy). Локальный
    троттлинг защищает и школу, и чужие учётные записи от случайного/злонамеренного
    перебора;
  * **одноразовый код подписи** — код TOTP это 6 цифр (10^6 вариантов), и в окне
    ±1 шаг одновременно действительны три кода. Без ограничения частоты держащий
    чужой access-токен мог бы подобрать код перебором за несколько часов и подписать
    приказ/ознакомление от имени сотрудника (а подпись здесь — юридически значимая).
    Поэтому неудачные проверки кода и контрольного слова тоже троттлятся и пишутся
    в журнал событий (`SIGNATURE_FAILED`).

Реализация — in-memory (на один процесс uvicorn). Для кластера из нескольких реплик
замените хранилище на Redis с тем же интерфейсом (см. docs/DEPLOY-NAS.md).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.config import settings
from app.errors import AppError


@dataclass
class _Bucket:
    attempts: deque[float]


class AttemptLimiter:
    """Скользящее окно неудачных попыток: по учётной записи (жёстко) и по IP (мягче).

    Один класс на все сценарии перебора; текст сообщения задаётся `what`/`hint`,
    чтобы 429 объяснял человеку, что именно заблокировано и что делать.
    """

    def __init__(
        self,
        max_attempts: int | None = None,
        window_sec: int | None = None,
        *,
        what: str = "неудачных попыток входа",
        hint: str = "Если пароль забыт — обратитесь к администратору домена.",
    ) -> None:
        self.max_attempts = max_attempts if max_attempts is not None else settings.login_rate_limit
        self.window = window_sec if window_sec is not None else settings.login_rate_window_min * 60
        self.what = what
        self.hint = hint
        self._by_ip: dict[str, deque[float]] = defaultdict(deque)
        self._by_account: dict[str, deque[float]] = defaultdict(deque)
        self._last_gc = 0.0
        # потолок числа отслеживаемых ключей: память ограничена даже при флуде
        self.max_keys = 10_000

    def _prune(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()

    def _collect_garbage(self, now: float) -> None:
        """Убирает пустые корзины, чтобы словари не росли от внешнего ввода.

        Ключи — это IP-адреса и e-mail'ы, которые атакующий может подставлять
        произвольно: без сборки мусора каждая уникальная строка оставалась бы в
        `_by_ip`/`_by_account` навсегда (review/REVIEW.md §5.14, §P3-24).
        Вызывается не чаще раза в `window` — стоимость пропорциональна числу
        активных ключей, а не числу запросов.
        """
        self._last_gc = now
        for store in (self._by_ip, self._by_account):
            for key in [k for k, b in store.items() if not b or now - b[-1] > self.window]:
                store.pop(key, None)
            # жёсткий потолок на случай флуда уникальными ключами внутри одного окна
            while len(store) > self.max_keys:
                oldest = min(store, key=lambda k: store[k][-1] if store[k] else 0.0)
                store.pop(oldest, None)

    def check(self, ip: str | None, account: str) -> None:
        """Бросает 429, если лимит исчерпан. Вызывается ДО обращения в каталог."""
        if self.max_attempts <= 0:
            return
        now = time.monotonic()
        if now - self._last_gc > self.window:
            self._collect_garbage(now)
        key = account.lower()

        if ip:
            self._prune(self._by_ip[ip], now)
            if len(self._by_ip[ip]) >= self.max_attempts * 3:      # по IP лимит мягче (NAT/весь класс)
                raise self._too_many(ip)
        self._prune(self._by_account[key], now)
        if len(self._by_account[key]) >= self.max_attempts:
            raise self._too_many(key)

    def register_failure(self, ip: str | None, account: str) -> None:
        now = time.monotonic()
        self._by_account[account.lower()].append(now)
        if ip:
            self._by_ip[ip].append(now)

    def register_success(self, ip: str | None, account: str) -> None:
        self._by_account.pop(account.lower(), None)
        # счётчик по IP не сбрасываем: успех одного не отменяет перебор другого

    def _too_many(self, key: str) -> AppError:
        minutes = max(1, round(self.window / 60))
        detail = f"Слишком много {self.what}. Повторите через {minutes} мин."
        if self.hint:
            detail = f"{detail} {self.hint}"
        return AppError(status_code=429, detail=detail, code="RATE_LIMITED")

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        return {
            "tracked_accounts": sum(1 for b in self._by_account.values() if b and now - b[-1] < self.window),
            "tracked_ips": sum(1 for b in self._by_ip.values() if b and now - b[-1] < self.window),
            "keys_accounts": len(self._by_account),
            "keys_ips": len(self._by_ip),
            "max_keys": self.max_keys,
        }


# обратная совместимость: прежнее имя класса (его используют тесты и импорты)
LoginRateLimiter = AttemptLimiter

login_limiter = AttemptLimiter()

# Неудачные проверки одноразового кода / контрольного слова (ПЭП).
# Ключ — id пользователя: перебор ведёт тот, кто уже аутентифицирован (своим или
# украденным токеном), поэтому IP здесь не так важен, как учётная запись.
signature_limiter = AttemptLimiter(
    max_attempts=settings.totp_max_attempts,
    window_sec=settings.totp_lock_window_min * 60,
    what="неудачных попыток подписи кодом из аутентификатора",
    hint="Код живёт 30 секунд — сверьте время на телефоне и введите текущий код.",
)

# Аварийный сброс привязки аутентификатора Владельцем по контрольному слову (v1.12).
# Ограничение ЖЁСТЧЕ подписного: операция снимает электронную подпись Владельца
# и открывает доступ к восстановлению из копии, очистке журнала и локдауну,
# поэтому окно подбора слова должно быть узким (5 попыток / 30 минут).
break_glass_limiter = AttemptLimiter(
    max_attempts=settings.break_glass_max_attempts,
    window_sec=settings.break_glass_window_min * 60,
    what="неудачных попыток аварийного сброса",
    hint="Проверьте пароль и контрольное слово; при утере слова доступ восстанавливается "
         "из резервной копии базы (см. docs/DEPLOY-NAS.md).",
)
