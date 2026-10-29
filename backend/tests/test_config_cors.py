"""Разбор CORS_ORIGINS из окружения (v1.10): JSON, список через запятую, пусто.

Опечатка в .env на NAS не должна ронять старт бэкенда: pydantic-settings требует
от списков строгий JSON ещё до валидаторов, поэтому в app/config.py источник
окружения расширен «мягким» разбором (см. _LenientComplexMixin).
"""
from __future__ import annotations

import pytest

from app.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[str]:
    if value is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", value)
    return Settings(_env_file=None).cors_origins


def test_cors_json_list(monkeypatch):
    assert _settings(monkeypatch, '["https://a.ru", "https://b.ru:9090"]') == [
        "https://a.ru",
        "https://b.ru:9090",
    ]


def test_cors_comma_separated(monkeypatch):
    assert _settings(monkeypatch, "https://a.ru,https://b.ru:9090") == [
        "https://a.ru",
        "https://b.ru:9090",
    ]


def test_cors_empty_means_no_external_origins(monkeypatch):
    assert _settings(monkeypatch, "") == []


def test_cors_default_dev_origins(monkeypatch):
    assert _settings(monkeypatch, None) == ["http://localhost:5173", "http://localhost:4173"]


def test_cors_single_origin_no_brackets(monkeypatch):
    assert _settings(monkeypatch, "https://atom-school.local:9090") == ["https://atom-school.local:9090"]
