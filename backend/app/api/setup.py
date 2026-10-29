"""Первоначальная настройка через веб-интерфейс (без терминала).

Публичные эндпоинты (доступны до создания первого Владельца):
  GET  /setup/status          — нужна ли ещё настройка
  POST /setup/first-owner     — создать Владельца (требуется одноразовый код из журнала)
  POST /setup/token           — показать код повторно (только из локальной сети NAS)
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import forbidden
from app.schemas.user import UserOut
from app.security import SessionDep
from app.services import audit, bootstrap
from app.services.bootstrap import OwnerCredentials
from app.version import VERSION

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupStatusOut(BaseModel):
    is_configured: bool
    setup_required: bool
    school_name: str | None = None
    completed_by: str | None = None
    hint: str = ""


class FirstOwnerIn(BaseModel):
    setup_token: str = Field(min_length=4, max_length=32, description="Одноразовый код из журнала контейнера")
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    school_name: str | None = Field(default=None, max_length=200)


class SetupTokenOut(BaseModel):
    setup_token: str
    hint: str = ""


def _is_local(request: Request) -> bool:
    """Запрос с того же хоста, где работает backend (консоль NAS, docker exec).

    За reverse-proxy `request.client.host` — это адрес контейнера nginx, поэтому
    наружу код настройки таким способом не отдаётся.
    """
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


@router.get("/status", response_model=SetupStatusOut, summary="Состояние первоначальной настройки")
async def status(session: SessionDep) -> SetupStatusOut:
    configured = await bootstrap.is_configured(session)
    state = await bootstrap.get_setup_state(session)
    return SetupStatusOut(
        is_configured=configured,
        setup_required=not configured,
        school_name=state.school_name,
        completed_by=state.completed_by_email,
        hint=(
            "Система настроена"
            if configured
            else "Скопируйте код настройки из журнала контейнера backend и создайте учётную запись Владельца"
        ),
    )


@router.post("/first-owner", response_model=UserOut, status_code=201, summary="Создать первого Владельца")
async def first_owner(payload: FirstOwnerIn, request: Request, session: SessionDep) -> UserOut:
    owner = await bootstrap.create_first_owner(
        session,
        OwnerCredentials(
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            school_name=payload.school_name,
        ),
        token=payload.setup_token,
    )
    # немедленно снимаем блокировку API (иначе пришлось бы ждать сброса кэша)
    request.app.state.setup_complete = True

    await audit.write_audit(
        session,
        actor=owner,
        action="SETUP_COMPLETE",
        entity_type="user",
        entity_id=owner.id,
        changes={"email": owner.email, "role": owner.role.value},
        request=request,
    )
    return UserOut.model_validate(owner)


@router.post("/token", response_model=SetupTokenOut, summary="Показать код настройки (только с локального хоста)")
async def show_token(session: SessionDep, request: Request) -> SetupTokenOut:
    """Удобно на самом NAS: `curl -X POST http://localhost:8080/api/v1/setup/token`.

    Из внешней сети не работает — код виден только в журнале контейнера.
    """
    if not _is_local(request):
        raise forbidden("Код настройки можно получить только на самом сервере (localhost) или в журнале контейнера")
    if await bootstrap.is_configured(session):
        raise forbidden("Система уже настроена")
    state = await bootstrap.get_setup_state(session)
    return SetupTokenOut(
        setup_token=state.setup_token or "",
        hint="Введите этот код на странице первоначальной настройки",
    )


@router.get("/info", summary="Служебная информация об установке")
async def info(session: SessionDep) -> dict:
    """Не содержит секретов: только режим работы и признак автогенерации ключа."""
    return {
        "app_env": settings.app_env,
        "version": VERSION,                     # та же версия, что отдаёт /health (§5.10)
        "secret_key_source": "env" if os.getenv("SECRET_KEY") else "auto-generated file",
        "ldap_enabled": settings.ldap_enabled,
        "demo_data": settings.auto_seed,
    }
