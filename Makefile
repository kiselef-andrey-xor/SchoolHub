.PHONY: help up down logs seed test smoke dev-api dev-web build-web install clean \
        up-nas down-nas logs-nas doctor backup build-arm \
        lint typecheck check sync-compose revision migrate precommit secrets

# каталог данных на Synology (переопределяется в .env проекта)
DATA_ROOT ?= /volume1/docker/schoolhub
COMPOSE_NAS := docker compose -f deploy/synology/docker-compose.yml --env-file $(DATA_ROOT)/.env

help: ## Показать справку
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- локальная разработка
up: ## Поднять dev-стенд в Docker (Postgres + API + Vite)
	docker compose up --build -d
	@echo "Frontend: http://localhost:5173  |  API docs: http://localhost:8000/docs"

down: ## Остановить dev-стенд
	docker compose down

logs: ## Логи backend'а (dev)
	docker compose logs -f backend

seed: ## (Пере)засеять демо-данные
	cd backend && python -m scripts.seed --force

test: ## Интеграционные тесты backend'а
	cd backend && python -m pytest -q

smoke: ## Сквозной smoke-тест всех сценариев
	cd backend && python scripts/smoke_test.py

lint: ## Линтеры: ruff (backend) + tsc --noEmit (frontend)
	cd backend && ruff check app scripts tests
	cd frontend && npx tsc --noEmit

typecheck: ## Только типы фронтенда (tsc --noEmit)
	cd frontend && npx tsc --noEmit

check: ## Всё, что гоняет CI: зависимости → импорт → миграции → тесты → типы → сборка
	bash scripts/ci-check.sh

precommit: ## Установить git-хуки (ruff, дрейф схемы, секреты, tsc) — review §6.2
	@command -v pre-commit >/dev/null 2>&1 || pip install --quiet pre-commit
	pre-commit install
	pre-commit install --hook-type pre-push
	@echo "Хуки установлены. Прогнать всё вручную: pre-commit run --all-files"
	@echo "Обойти для одного коммита (не рекомендуется): git commit --no-verify"

secrets: ## Проверить tracked-файлы на захардкоженные секреты и артефакты сборки
	bash scripts/check-no-secrets.sh
	bash scripts/check-no-build-artifacts.sh

sync-compose: ## Синхронизировать deploy/synology/docker-compose.yml с корневым
	@{ printf '%s\n' \
	    "# ЗЕРКАЛЬНАЯ КОПИЯ корневого docker-compose.yml — НЕ редактируйте этот файл." \
	    "# Правки вносите в корневой docker-compose.yml, затем выполните:  make sync-compose" \
	    "# (цель просто копирует корневой файл сюда, поэтому расхождение невозможно)." \
	    "# В мастере Container Manager можно выбирать любой из двух файлов — они идентичны." \
	    ""; cat docker-compose.yml; } > deploy/synology/docker-compose.yml
	@echo "deploy/synology/docker-compose.yml синхронизирован с корневым"
	@git diff --quiet deploy/synology/docker-compose.yml || echo "  (есть изменения — не забудьте закоммитить)"

revision: ## Создать миграцию: make revision m="описание изменения"
	cd backend && alembic revision --autogenerate -m "$(m)"

migrate: ## Применить миграции схемы (alembic upgrade head)
	cd backend && alembic upgrade head

dev-api: ## Backend с автоперезагрузкой
	cd backend && uvicorn app.main:app --reload --port 8000

dev-web: ## Frontend dev server
	cd frontend && npm run dev

build-web: ## Production-сборка фронтенда
	cd frontend && npm run build

install: ## Установить зависимости локально
	cd backend && pip install -r requirements.txt
	cd frontend && npm install

# ---------------------------------------------------------------- Synology NAS (arm64)
up-nas: ## Собрать и запустить SchoolHub на NAS (deploy/synology)
	$(COMPOSE_NAS) up -d --build
	@$(MAKE) --no-print-directory doctor

down-nas: ## Остановить стек на NAS
	$(COMPOSE_NAS) down

logs-nas: ## Логи backend'а на NAS
	docker logs -f schoolhub-backend

doctor: ## Диагностика развёртывания: контейнеры, БД, LDAP, память, место
	bash deploy/synology/check-health.sh

backup: ## Бэкап БД (pg_dump + sha256 + ротация)
	bash deploy/synology/backup-db.sh

build-arm: ## Проверить сборку образов под linux/arm64 на x86-машине (buildx/qemu)
	docker buildx build --platform linux/arm64 -t schoolhub-backend:arm64-test backend
	docker buildx build --platform linux/arm64 --target prod -t schoolhub-frontend:arm64-test frontend

clean: ## Удалить артефакты сборки
	rm -rf frontend/dist backend/.pytest_cache frontend/tsconfig.tsbuildinfo
	find . -type d -name __pycache__ -not -path "*/node_modules/*" -exec rm -rf {} +
