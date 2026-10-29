"""Типовые исключения приложения -> HTTP-коды."""
from __future__ import annotations

from fastapi import HTTPException, status


class AppError(HTTPException):
    def __init__(self, status_code: int, detail: str, code: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code or detail


def not_found(entity: str = "Объект") -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, f"{entity} не найден")


def forbidden(detail: str = "Недостаточно прав", code: str = "FORBIDDEN") -> AppError:
    """403. `code` уточняет причину для UI: например TICKET_INVALID заставляет
    фронтенд заново запросить подпись, а не показывать «доступ запрещён»."""
    return AppError(status.HTTP_403_FORBIDDEN, detail, code=code)


def conflict(detail: str) -> AppError:
    return AppError(status.HTTP_409_CONFLICT, detail, code="CONFLICT")


def bad_request(detail: str) -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, detail, code="BAD_REQUEST")


def unauthorized(detail: str = "Не авторизован") -> AppError:
    return AppError(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        code="UNAUTHORIZED",
    )
