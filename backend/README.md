# SchoolHub · Backend (FastAPI)

Полная документация проекта — в корневом [`README.md`](../README.md),
архитектура — в [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md),
контракт API — в [`docs/API.md`](../docs/API.md).

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                   # задайте SECRET_KEY и DATABASE_URL

# PostgreSQL (или SQLite для быстрой проверки: sqlite+aiosqlite:///./dev.db)
export DATABASE_URL="postgresql+asyncpg://schoolhub:schoolhub@localhost:5432/schoolhub"

python -m scripts.seed --force                          # схема + демо-данные (PDF/DOCX-приказы)
uvicorn app.main:app --reload --port 8000               # http://localhost:8000/docs
```

## Проверки

```bash
pytest -q                     # 104 интеграционных теста (ASGI + in-memory SQLite)
python scripts/smoke_test.py  # 151 сквозная проверка сценариев и запретов RBAC
python scripts/debug_api.py   # ручной стенд: дёргает ключевые эндпоинты и печатает ответы
```

## Структура

```
app/
├── api/            # HTTP-слой: маршруты + RBAC-зависимости + сериализация
├── services/       # бизнес-логика (orders, tasks, notifications, audit, storage, documents)
├── models/         # SQLAlchemy ORM
├── schemas/        # Pydantic-контракты
├── security.py     # JWT, bcrypt, require_role(), get_current_user
├── capabilities.py # матрица «роль → возможности» (единый источник для API и UI)
├── enums.py        # справочники и state machine статусов
├── types.py        # портируемые типы колонок (UUID, Enum→VARCHAR)
├── dbutils.py      # переносимый регистронезависимый поиск (PostgreSQL ILIKE ↔ SQLite)
├── errors.py       # бизнес-исключения → HTTP-коды
├── config.py       # настройки из ENV
└── main.py         # фабрика приложения, middleware, обработчики ошибок
scripts/            # seed демо-данных, генератор PDF/DOCX, smoke-тест, отладочный стенд
tests/              # интеграционные тесты
assets/fonts/       # Noto Sans (OFL) — только для генерации демо-документов
storage/uploads/    # файловое хранилище (в git не попадает)
```

## Полезные ENV

| Переменная | Описание |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…` или `sqlite+aiosqlite:///…` |
| `SECRET_KEY` | подпись JWT (в prod — только из secret-хранилища) |
| `UPLOAD_DIR`, `MAX_UPLOAD_MB` | корень хранилища документов и лимит размера |
| `AUTO_SEED` | засеять демо-данные при старте приложения (dev) |
| `APP_ENV=prod` | отключает Swagger и `create_all` (схема управляется миграциями) |

## Миграции (prod)

```bash
pip install alembic
alembic init -t async migrations
# в migrations/env.py: target_metadata = app.database.Base.metadata
alembic revision --autogenerate -m "initial"
alembic upgrade head
```
