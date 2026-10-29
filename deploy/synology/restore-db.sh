#!/bin/bash
# ============================================================================
#  Восстановление БД SchoolHub из дампа (plain SQL, создан pg_dump / кнопкой
#  «Резервная копия» в интерфейсе или backup-db.sh).
#
#  Применение:
#    sudo bash restore-db.sh /volume1/backup/schoolhub/schoolhub-20261013-033000.sql
#
#  ВНИМАНИЕ: восстановление ЗАМЕЩАЕТ текущую базу. Перед ним остановите backend,
#  чтобы приложение не писало в БД во время наката:
#    cd /volume2/docker/schoolhub/app
#    sudo docker compose -f deploy/synology/docker-compose.yml stop backend frontend
#    sudo bash restore-db.sh <файл дампа>
#    sudo docker compose -f deploy/synology/docker-compose.yml start backend frontend
#
#  Дамп накатывается на ЧИСТУЮ схему: скрипт сам сбрасывает схему public.
#  Если нужна текущая база «на потом» — сначала снимите копию кнопкой в UI
#  или backup-db.sh.
# ============================================================================
set -euo pipefail

DUMP="${1:-}"
CONTAINER="${CONTAINER:-schoolhub-db}"
DB_USER="${DB_USER:-schoolhub}"
DB_NAME="${DB_NAME:-schoolhub}"

if [[ -z "$DUMP" || ! -f "$DUMP" ]]; then
    echo "Укажите существующий файл дампа: sudo bash restore-db.sh /path/to/backup.sql" >&2
    exit 1
fi

if ! sudo -n docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    echo "Контейнер ${CONTAINER} не запущен." >&2
    exit 1
fi

echo ">> Бэкап текущей базы на всякий случай: ${DUMP}.pre-restore"
# sudo не распространяется на перенаправление (shellcheck SC2024) — пишем через tee.
# И обязательно проверяем результат: сбрасывать схему public, не сняв свежую копию, нельзя
# (pipefail включён, поэтому статус берётся именно у pg_dump, а не у tee).
if ! sudo docker exec "$CONTAINER" pg_dump -U "$DB_USER" --no-owner --no-privileges "$DB_NAME" \
     | sudo tee "${DUMP}.pre-restore" >/dev/null; then
    echo "Не удалось снять бэкап текущей базы — восстановление прервано, схема не тронута." >&2
    exit 1
fi

echo ">> Сброс схемы public…"
sudo docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 <<'SQL'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
SQL

echo ">> Накат дампа ${DUMP}…"
sudo docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 < "$DUMP"

echo ">> Готово. Запустите backend/frontend и проверьте вход."
