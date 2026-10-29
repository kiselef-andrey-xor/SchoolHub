"""Троттлинг попыток входа: ключи не должны расти от внешнего ввода (review §5.14).

`LoginRateLimiter` — in-memory: ключами служат IP-адреса и e-mail'ы, которые
атакующий может подставлять произвольно. Без сборки мусора каждая уникальная
строка оставалась бы в словаре навсегда, то есть перебор превращался бы ещё
и в исчерпание памяти процесса.
"""
from __future__ import annotations

import pytest

from app.services.rate_limit import LoginRateLimiter


def test_limiter_blocks_after_max_attempts():
    limiter = LoginRateLimiter(max_attempts=3, window_sec=60)
    for _ in range(3):
        limiter.check("10.0.0.1", "teacher@school.ru")
        limiter.register_failure("10.0.0.1", "teacher@school.ru")
    with pytest.raises(Exception) as exc:
        limiter.check("10.0.0.1", "teacher@school.ru")
    assert exc.value.status_code == 429
    assert exc.value.code == "RATE_LIMITED"

    # успешный вход сбрасывает счётчик учётки (но не счётчик IP)
    limiter.register_success("10.0.0.1", "teacher@school.ru")
    limiter.check("10.0.0.1", "teacher@school.ru")


def test_empty_buckets_are_collected():
    """Пустые корзины удаляются: словарь не растёт числом уникальных ключей."""
    limiter = LoginRateLimiter(max_attempts=3, window_sec=1)
    for i in range(50):
        limiter.register_failure(f"10.0.0.{i}", f"user{i}@school.ru")
    assert limiter.stats()["keys_accounts"] == 50

    # окно (1 с) истекло → следующий check запускает сборку мусора
    limiter._last_gc -= 10
    limiter.window = 0  # всё считается протухшим
    limiter.check("10.0.0.255", "probe@school.ru")

    stats = limiter.stats()
    assert stats["keys_accounts"] <= 1, stats
    assert stats["keys_ips"] <= 1, stats


def test_number_of_tracked_keys_is_capped():
    """Флуд уникальными ключами внутри одного окна упирается в потолок памяти."""
    limiter = LoginRateLimiter(max_attempts=3, window_sec=3600)
    limiter.max_keys = 20
    for i in range(200):
        limiter.register_failure(f"10.1.{i // 250}.{i % 250}", f"victim{i}@school.ru")
        limiter.check(f"10.1.{i // 250}.{i % 250}", f"victim{i}@school.ru")
        limiter._last_gc -= 3601  # каждый проход — с уборкой
    assert limiter.stats()["keys_accounts"] <= limiter.max_keys
    assert limiter.stats()["keys_ips"] <= limiter.max_keys


def test_limit_can_be_disabled():
    limiter = LoginRateLimiter(max_attempts=0, window_sec=60)
    for _ in range(50):
        limiter.register_failure("10.0.0.9", "x@school.ru")
    limiter.check("10.0.0.9", "x@school.ru")   # не бросает 429
