"""Точка входа FastAPI: фабрика приложения, middleware, обработчики ошибок, OpenAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api import api_router
from app.config import settings
from app.database import SessionLocal, init_db
from app.errors import AppError

logger = logging.getLogger("schoolhub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from app.version import VERSION  # единый источник: /health и /setup/info отдают одно и то же


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import SessionLocal
    from app.services import bootstrap

    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)

    # схема: `alembic upgrade head` + идемпотентные шаги app/migrations.py (см. init_db);
    # AUTO_MIGRATE=false отключает автоприменение — тогда миграции накатывает DBA вручную
    await init_db()
    logger.info("DB schema ready")

    async with SessionLocal() as session:
        # владелец из ENV (OWNER_EMAIL/OWNER_PASSWORD) — для установки без веб-интерфейса
        await bootstrap.ensure_owner_from_env(session)

        # системные карточки справки (модуль 4 ТЗ) — идемпотентный seed
        from app.services import help as help_service

        if await help_service.seed_system_articles(session):
            logger.info("Справка: созданы недостающие системные карточки")
            await session.commit()

        if await bootstrap.is_configured(session):
            logger.info("Система настроена — доступен обычный вход")
        else:
            state = await bootstrap.get_setup_state(session)
            bootstrap.print_setup_banner(state.setup_token or "")
            await session.commit()

    from app.services import reminders as reminders_service

    reminders_task = reminders_service.start_reminders()

    if settings.auto_seed:
        from scripts.seed import seed_all

        async with SessionLocal() as session:
            created = await seed_all(session, force=False)
            await session.commit()
        logger.info("Seed: %s", "applied" if created else "skipped (data exists)")

    yield
    if reminders_task is not None:
        reminders_task.cancel()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "prod" else None,
        summary="Единая цифровая среда школы: приказы с электронным ознакомлением и Task Manager с RBAC.",
        description=(
            "## Роли\n"
            "* **OWNER (Владелец)** — полный доступ, управление пользователями и ролями.\n"
            "* **ADMIN (Администрация)** — приказы (загрузка/редактирование), все задачи, чтение аудита.\n"
            "* **TEACHER (Учитель)** — опубликованные приказы + подтверждение ознакомления, только свои задачи.\n\n"
            "Права проверяются на уровне API (RBAC-зависимости) и дублируются в UI через capabilities."
        ),
    )

    # `CORS_ORIGINS=*` вместе с allow_credentials=True — это дыра, а не «широкая
    # настройка»: Starlette в таком сочетании подставляет в Access-Control-Allow-Origin
    # ЛЮБОЙ запрошенный Origin, и любой сайт читает ответы API вместе с cookie
    # пользователя. Поэтому «звёздочка» игнорируется с внятной записью в журнал:
    # приложение стартует, но кросс-доменный доступ не открывается.
    cors_origins = [o.strip() for o in settings.cors_origins if o.strip() and o.strip() != "*"]
    if "*" in {o.strip() for o in settings.cors_origins}:
        logger.error(
            "CORS_ORIGINS содержит '*' — значение проигнорировано: вместе с allow_credentials "
            "оно открыло бы API любому сайту. Перечислите адреса явно, например "
            "CORS_ORIGINS='[\"https://school.example.ru\"]'"
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        expose_headers=["Content-Disposition", "X-Order-Number"],
        max_age=600,
    )

    # ---------------------------------------------------------------------
    # Защита «API закрыт до первоначальной настройки».
    #
    # Состояние держим в app.state, а не в глобальном SessionLocal, потому что
    # в тестах (и за reverse-proxy) источник сессий может быть переопределён.
    # После создания Владельца флаг выставляется явно — повторные запросы к БД
    # не выполняются вовсе.
    # ---------------------------------------------------------------------
    SETUP_PREFIXES = (f"{settings.api_prefix}/setup",)
    # healthcheck обязателен ДО завершения настройки: его опрашивают Docker/DSM,
    # иначе контейнер посчитают «нездоровым» и не поднимут стек
    SETUP_EXACT = {"/health", f"{settings.api_prefix}/health"}
    if settings.app_env != "prod":
        SETUP_EXACT |= {"/docs", "/openapi.json", "/redoc", "/", "/docs/oauth2-redirect"}

    app.state.setup_complete = False
    app.state.session_factory = SessionLocal

    async def is_setup_complete() -> bool:
        """True, если первый Владелец уже создан (проверяется один раз)."""
        if app.state.setup_complete:
            return True
        from app.services.bootstrap import is_configured

        factory = getattr(app.state, "session_factory", None) or SessionLocal
        try:
            async with factory() as session:
                done = await is_configured(session)
        except Exception as exc:  # БД ещё не готова / нет таблицы — считаем «не настроено»
            logger.debug("setup check failed: %r", exc)
            return False
        app.state.setup_complete = bool(done)
        return bool(done)

    app.state.is_setup_complete = is_setup_complete

    @app.middleware("http")
    async def setup_guard(request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        # ВАЖНО: сравнение точное, а не startswith — префикс "/" открыл бы всё приложение
        if path in SETUP_EXACT or path.startswith(SETUP_PREFIXES) or request.method == "OPTIONS":
            return await call_next(request)
        if await is_setup_complete():
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Система ещё не настроена: откройте веб-интерфейс и создайте учётную запись Владельца",
                "code": "SETUP_REQUIRED",
            },
        )

    # Пути, которым нужна «разрешающая» CSP: Swagger UI работает на inline-скриптах
    # и внешних стилях. В prod документация отключена (docs_url=None), так что
    # список пустой, но в dev/staging строгая политика сломала бы /docs.
    CSP_EXEMPT = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Базовая CSP для ответов API. Смысл: JSON/CSV/файл, открытый напрямую
        # в браузере, не должен уметь выполнять скрипты (например, когда выгрузка
        # сохранена как .html или браузер ошибочно снаффит содержимое).
        # setdefault — печатные формы задают свою политику (app/htmlsafe.py),
        # а статику закрывает nginx (frontend/security-headers.conf).
        if request.url.path not in CSP_EXEMPT:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
        return response

    # ------------------------------------------------------------- exceptions
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": getattr(exc, "code", None)},
        )

    @app.exception_handler(IntegrityError)
    async def integrity_handler(_: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("IntegrityError: %s", exc.orig)
        return JSONResponse(status_code=409, content={"detail": "Нарушение целостности данных", "code": "CONFLICT"})

    @app.exception_handler(SQLAlchemyError)
    async def db_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("Database error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Ошибка базы данных", "code": "DB_ERROR"})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера", "code": "INTERNAL"})

    # ----------------------------------------------------------------- routes
    from app.api.setup import router as setup_router

    app.include_router(setup_router, prefix=settings.api_prefix)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["system"], summary="Healthcheck")
    async def health() -> dict:
        return {"status": "ok", "app": settings.app_name, "env": settings.app_env, "version": VERSION}

    @app.get("/", tags=["system"], include_in_schema=False)
    async def root() -> dict:
        return {
            "app": settings.app_name,
            "version": VERSION,
            "docs": "/docs" if settings.app_env != "prod" else "disabled",
            "api": settings.api_prefix,
        }

    return app


app = create_app()
