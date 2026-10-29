"""Портируемые типы колонок.

UUID хранится нативно в PostgreSQL (dialect_impl) и как CHAR(36) в SQLite —
это позволяет гонять интеграционные тесты на in-memory SQLite без изменения
моделей и без «продакшн-only» кода.

EnumString хранит enum как VARCHAR, но в Python всегда отдаёт экземпляр Enum.
Это избавляет от класса ошибок «иногда str, иногда Enum» (который неизбежно
возникает при String-колонке + Mapped[SomeEnum]).
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import CHAR, String, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


class GUID(TypeDecorator):
    """Platform-independent UUID type."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value if dialect.name == "postgresql" else str(value)
        return uuid.UUID(str(value)) if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class EnumString(TypeDecorator):
    """Enum -> VARCHAR в БД, VARCHAR -> Enum в Python."""

    impl = String
    cache_ok = True

    def __init__(self, enum_type: type[enum.Enum], length: int = 40, **kw) -> None:
        self.enum_type = enum_type
        super().__init__(length=length, **kw)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, enum.Enum) else self.enum_type(value).value

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, self.enum_type):
            return value
        return self.enum_type(value)


def enum_value(value) -> str:
    """Безопасное получение строкового значения (Enum | str | None)."""
    if value is None:
        return ""
    return value.value if isinstance(value, enum.Enum) else str(value)
