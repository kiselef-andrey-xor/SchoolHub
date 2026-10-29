#!/usr/bin/env bash
# ============================================================================
#  SchoolHub — локальный прогон всех проверок CI (make check).
#
#  Зачем этот скрипт существует: три P0-дефекта, найденных в review/REVIEW.md,
#  имеют одно происхождение — изменение проверили на машине разработчика, где
#  окружение уже содержало нужные пакеты. Шаг 1 («чистый venv → импорт app.main»)
#  ловит ровно этот класс ошибок и стоит 30 секунд.
#
#  Использование:
#      bash scripts/ci-check.sh              # всё
#      SKIP_FRONTEND=1 bash scripts/ci-check.sh
#      SKIP_CLEAN_VENV=1 bash scripts/ci-check.sh   # не пересоздавать venv (быстрее)
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."   # корень репозитория: все пути ниже относительные

# alembic читает alembic.ini в кодировке ЛОКАЛИ (configparser encoding="locale"),
# а в файле есть кириллица. Без UTF-8-локали это UnicodeDecodeError.
# См. review/REVIEW.md §5.2 — Dockerfile задаёт LANG/PYTHONUTF8, здесь делаем то же.
export LC_ALL="${LC_ALL:-C.UTF-8}"
export LANG="${LANG:-C.UTF-8}"
export PYTHONUTF8=1

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

# ---------------------------------------------------------------- 1. чистое окружение
if [[ "${SKIP_CLEAN_VENV:-0}" != "1" ]]; then
  step "1/7 Чистый venv: установка зависимостей из requirements.txt"
  VENV="$(mktemp -d)/venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --disable-pip-version-check --upgrade pip
  "$VENV/bin/pip" install -q --disable-pip-version-check -r backend/requirements.txt
  PY="$VENV/bin/python"
else
  step "1/7 Чистый venv пропущен (SKIP_CLEAN_VENV=1)"
  PY="${PYTHON:-python3}"
  VENV=""     # set -u: переменная обязана существовать, даже если venv не создавался
fi

# ---------------------------------------------------------------- 2. приложение импортируется
step "2/7 app.main импортируется на ГОЛОМ requirements.txt"
# Ловит потерянные зависимости. Именно так в main@8980058 уехал cryptography:
# app/services/totp.py импортирует Fernet, а в requirements.txt его не было.
(cd backend && "$PY" -c "import app.main" ) \
  || fail "app.main не импортируется — проверьте, все ли зависимости объявлены в requirements.txt"

# ---------------------------------------------------------------- 3. линтер
step "3/7 ruff (backend)"
if command -v ruff >/dev/null 2>&1; then
  (cd backend && ruff check app scripts tests) || fail "ruff нашёл замечания"
elif [[ -n "$VENV" && -x "$VENV/bin/ruff" ]]; then
  (cd backend && "$VENV/bin/ruff" check app scripts tests) || fail "ruff нашёл замечания"
else
  "$PY" -m pip install -q --disable-pip-version-check ruff
  (cd backend && "$PY" -m ruff check app scripts tests) || fail "ruff нашёл замечания"
fi

# ---------------------------------------------------------------- 4. тесты
step "4/7 pytest (backend)"
(cd backend && "$PY" -m pytest -q) || fail "тесты упали"

# ---------------------------------------------------------------- 5. схема: alembic == модели
step "5/7 alembic upgrade head совпадает с моделями (без дрейфа схемы)"
# Отдельный файл, а не часть pytest: нужен пустой файл БД на диске.
(cd backend && "$PY" -m pytest tests/test_schema_drift.py -q) \
  || fail "схема после alembic upgrade head расходится с моделями — добавьте ревизию"

# ---------------------------------------------------------------- 6. фронтенд
if [[ "${SKIP_FRONTEND:-0}" != "1" ]]; then
  step "6/7 frontend: tsc --noEmit + сборка"
  (cd frontend && npm ci --no-audit --no-fund && npx tsc --noEmit && npm run build) \
    || fail "frontend: ошибки типов или сборки"
else
  step "6/7 frontend пропущен (SKIP_FRONTEND=1)"
fi

# ---------------------------------------------------------------- 7. гигиена репозитория
step "7/7 В отслеживаемых файлах нет секретов и артефактов сборки"
# Правила живут в scripts/check-no-*.sh — их же вызывают git-хуки и CI,
# поэтому проверка не может разъехаться в трёх местах.
bash scripts/check-no-build-artifacts.sh || fail "в репозитории есть артефакты сборки"
bash scripts/check-no-secrets.sh --all-tracked || fail "в отслеживаемых файлах найдены секреты"

printf '\n\033[1;32m✓ Все проверки CI пройдены\033[0m\n'
