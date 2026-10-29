"""Общие фикстуры: изолированная in-memory SQLite + ASGI-клиент + демо-пользователи.

Тесты проверяют не «идеальный» слой сервисов, а реальный HTTP-контракт
приложения вместе с RBAC-зависимостями FastAPI.
"""
from __future__ import annotations

import io
import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# хранилище файлов — во временную директорию (до импорта app.services.storage)
os.environ.setdefault("UPLOAD_DIR", "/tmp/schoolhub-test-uploads")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only-32chars")

from app import models as _all_models  # noqa: E402,F401  — регистрация всех таблиц в metadata
from app import security as _security  # noqa: E402
from app.database import Base, get_session  # noqa: E402
from app.enums import Role  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.user import User  # noqa: E402

DEMO_PASSWORD = "Test1234!"

PEOPLE = [
    ("owner@example.com", "Владелец Один", Role.OWNER, "Учредитель"),
    ("admin@example.com", "Админ Один", Role.ADMIN, "Завуч"),
    ("teacher1@example.com", "Учитель Первый", Role.TEACHER, "Учитель математики"),
    ("teacher2@example.com", "Учитель Второй", Role.TEACHER, "Учитель физики"),
]


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """В тестах не жжём CPU на bcrypt: детерминированный «быстрый» хеш.

    Проверяется контракт (hash -> verify), а не криптостойкость.
    """
    import hashlib

    from app import security

    monkeypatch.setattr(security, "hash_password", lambda raw: "test$" + hashlib.sha256(raw.encode()).hexdigest())
    monkeypatch.setattr(
        security, "verify_password", lambda raw, hashed: hashed == "test$" + hashlib.sha256(raw.encode()).hexdigest()
    )


@pytest.fixture(autouse=True)
def reset_attempt_limiters():
    """Лимитеры попыток — in-memory и общие на процесс, поэтому чистим их между тестами.

    Иначе пять неудачных кодов подписи из одного теста заблокировали бы следующий
    (429 RATE_LIMITED), а тесты начали бы влиять друг на друга.
    """
    from app.services.rate_limit import break_glass_limiter, login_limiter, signature_limiter

    def _clear() -> None:
        # break_glass_limiter (v1.12) — отдельный лимитер аварийного сброса
        # привязки Владельца: его счётчик тоже обязан обнуляться между тестами,
        # иначе подбор слова из одного теста заблокировал бы следующий (429)
        for limiter in (login_limiter, signature_limiter, break_glass_limiter):
            limiter._by_ip.clear()
            limiter._by_account.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
async def engine():
    from app.dbutils import register_sqlite_functions

    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    register_sqlite_functions(eng)          # Unicode-aware поиск (аналог PostgreSQL ILIKE)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_seed_order_types)
        await conn.run_sync(_seed_positions)
    yield eng
    await eng.dispose()


def _seed_positions(sync_conn) -> None:
    """Тесты создают схему в обход миграций — сидируем справочник должностей."""
    from sqlalchemy import insert, select

    from app.models.position import Position
    from app.services.positions import SEED_POSITIONS

    existing = {r[0] for r in sync_conn.execute(select(Position.title)).all()}
    for i, (title, aliases) in enumerate(SEED_POSITIONS, start=1):
        if title not in existing:
            sync_conn.execute(insert(Position).values(title=title, aliases=aliases, sort_order=i))


def _seed_order_types(sync_conn) -> None:
    """Тесты создают схему в обход миграций — сидируем справочник типов приказов."""
    from sqlalchemy import insert, select

    from app.migrations import SEED_ORDER_TYPES
    from app.models.order_type import OrderTypeRow

    existing = {r[0] for r in sync_conn.execute(select(OrderTypeRow.code)).all()}
    for i, (code, title) in enumerate(SEED_ORDER_TYPES):
        if code not in existing:
            sync_conn.execute(insert(OrderTypeRow).values(code=code, title=title, position=i))


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def app(session_factory, tmp_path):
    """Приложение с переопределённой сессией БД и изолированным файловым хранилищем."""
    from app.services import storage as storage_module

    storage_module.storage = storage_module.LocalFileStorage(tmp_path / "uploads")

    application = create_app()

    async def _override():
        async with session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    application.dependency_overrides[get_session] = _override

    # middleware первоначальной настройки должен смотреть в ТУ ЖЕ тестовую БД
    application.state.session_factory = session_factory
    # защита «API закрыт до создания Владельца» проверяется отдельно в tests/test_setup.py;
    # остальные тесты работают с уже настроенным стендом
    application.state.setup_complete = True

    # записи аудита в отдельной транзакции тоже должны попадать в тестовую БД
    from app.services import audit as audit_service

    audit_service.session_factory_override = session_factory

    # WebSocket-роут открывает сессию сам (вне dependency_overrides) —
    # для проверки «пользователь всё ещё активен» при подключении
    from app.api import realtime as realtime_api

    realtime_api.session_factory_override = session_factory

    yield application
    audit_service.session_factory_override = None
    realtime_api.session_factory_override = None
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as c:
        yield c


@pytest.fixture
async def raw_client(session_factory, tmp_path):
    """Клиент к «свежеустановленному» приложению: первоначальная настройка НЕ выполнена.

    Используется только в tests/test_setup.py — остальные тесты работают с уже
    настроенным стендом (фикстура `client`).
    """
    from app.services import storage as storage_module

    storage_module.storage = storage_module.LocalFileStorage(tmp_path / "uploads-raw")

    application = create_app()

    async def _override():
        async with session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    application.dependency_overrides[get_session] = _override
    application.state.session_factory = session_factory
    application.state.setup_complete = False

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test/api/v1") as c:
        yield c
    application.dependency_overrides.clear()


@pytest.fixture
async def db_session(app):
    """Прямой доступ к тестовой БД (например, чтобы «состарить» дедлайн задачи)."""
    override = app.dependency_overrides[get_session]
    agen = override()
    session = await agen.__anext__()
    try:
        yield session
    finally:
        await agen.aclose()


@pytest.fixture
async def seeded(session_factory) -> dict[str, str]:
    """Создаёт пользователей, возвращает {role_key: user_id}."""
    ids: dict[str, str] = {}
    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.position import Position

        for email, name, role, position in PEOPLE:
            user = User(
                email=email,
                # ВАЖНО: через атрибут модуля, а не `from app.security import hash_password`.
                # Фикстура fast_password_hashing подменяет именно атрибут модуля, а прямой
                # импорт зафиксировал бы настоящий bcrypt: хеши в БД и проверка пароля
                # при входе разошлись бы (и каждый логин жёг бы 100–300 мс CPU).
                password_hash=_security.hash_password(DEMO_PASSWORD),
                full_name=name,
                role=role,
                is_active=True,
            )
            if position:
                pos = (
                    await session.execute(select(Position).where(Position.title == position))
                ).scalars().first()
                if pos is None:
                    pos = Position(title=position, aliases="", sort_order=500)
                    session.add(pos)
                    await session.flush()
                user.positions = [pos]
            session.add(user)
            await session.flush()
            ids[role.value.lower() + ("_2" if role == Role.TEACHER and "teacher2" in email else "")] = str(user.id)
            ids[email] = str(user.id)
        await session.commit()
    return ids


async def login(client: AsyncClient, email: str, password: str = DEMO_PASSWORD) -> AsyncClient:
    """Логинит ПЕРЕДАННЫЙ клиент (мутирует его заголовок Authorization)."""
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


def new_client(client: AsyncClient) -> AsyncClient:
    """Новый экземпляр клиента к тому же приложению — чтобы иметь несколько сессий/ролей."""
    return AsyncClient(transport=client._transport, base_url=str(client.base_url))


async def export_ticket(client: AsyncClient, email: str, target: str = "db_backup") -> str:
    """Одноразовый билет на выгрузку данных: привязывает ПЭП и подписывает операцию.

    Выгрузка дампа БД и архива файлов — чувствительная операция (review/REVIEW.md §P1-4),
    поэтому GET-эндпоинты принимают только билет из POST /system/export-ticket.
    """
    from tests.test_totp_pep import sign

    _, sig = await sign(client, email)
    s = sig()
    resp = await client.post(
        "/system/export-ticket",
        json={"target": target, "pep_code": s["code"], "pep_word": s["word"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


async def auth(client: AsyncClient, email: str, password: str = DEMO_PASSWORD) -> AsyncClient:
    """Новый клиент, уже аутентифицированный под указанным пользователем."""
    return await login(new_client(client), email, password)


def pdf_bytes(text: str = "Демо-документ приказа SchoolHub") -> bytes:
    """Минимальный валидный PDF (одностраничный, с текстом)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:  # pragma: no cover
        return b"%PDF-1.4\n%stub\n"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 760, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def upload(name: str, data: bytes, mime: str) -> dict[str, Any]:
    return {"file": (name, io.BytesIO(data), mime)}
