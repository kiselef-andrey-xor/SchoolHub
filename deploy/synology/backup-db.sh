#!/bin/bash
# ============================================================================
#  Ежедневный бэкап БД SchoolHub (pg_dump) в общую папку NAS.
#
#  Установка на Synology:
#    1. скопируйте скрипт:  /volume1/docker/schoolhub/backup-db.sh
#    2. chmod +x /volume1/docker/schoolhub/backup-db.sh
#    3. DSM → Панель управления → Планировщик задач → Создать → Запланированная задача →
#       Скрипт, определённый пользователем:
#         Общий путь:      /volume1/docker/schoolhub/backup-db.sh
#         Пользователь:    root
#         Расписание:      ежедневно, 03:30
#    4. Папку с дампами (/volume1/backup/schoolhub) включите в Hyper Backup.
#
#  Хранение: KEEP_DAYS последних дампов + KEEP_WEEKLY недельных (по понедельникам).
# ============================================================================
set -euo pipefail

# --- настройки (можно переопределить переменными окружения) ------------------
DATA_ROOT="${DATA_ROOT:-/volume1/docker/schoolhub}"
BACKUP_DIR="${BACKUP_DIR:-/volume1/backup/schoolhub}"
PROJECT_FILE="${PROJECT_FILE:-$DATA_ROOT/deploy/synology/docker-compose.yml}"
CONTAINER="${CONTAINER:-schoolhub-db}"
KEEP_DAYS="${KEEP_DAYS:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-8}"

STAMP="$(date +%Y%m%d-%H%M%S)"
DOW="$(date +%u)"                      # 1 = понедельник
LOG="$DATA_ROOT/backup.log"

mkdir -p "$BACKUP_DIR"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# --- читаем параметры БД из .env (не хардкодим пароли) ----------------------
if [ -f "$DATA_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$DATA_ROOT/.env"; set +a
fi
DB_NAME="${POSTGRES_DB:-schoolhub}"
DB_USER="${POSTGRES_USER:-schoolhub}"

log "=== backup start: $DB_NAME -> $BACKUP_DIR ==="

# --- 1) логический дамп (переносимый, восстанавливается на любой версии) ----
DUMP="$BACKUP_DIR/schoolhub-$STAMP.sql.gz"
if docker exec -i "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --clean --if-exists 2>>"$LOG" | gzip -6 > "$DUMP"; then
  SIZE=$(du -h "$DUMP" | cut -f1)
  log "OK  дамп создан: $DUMP ($SIZE)"
else
  log "ERR дамп НЕ создан (проверьте, что контейнер $CONTAINER запущен)"
  rm -f "$DUMP"
  exit 1
fi

# --- 2) контрольная сумма (целостность при восстановлении) ------------------
sha256sum "$DUMP" > "$DUMP.sha256"

# --- 3) копия дампа с пометкой «недельный» (хранится дольше) ----------------
if [ "$DOW" = "1" ]; then
  cp -f "$DUMP" "$BACKUP_DIR/weekly-schoolhub-$STAMP.sql.gz"
  log "OK  недельная копия сохранена"
fi

# --- 4) проверка восстанавливаемости (быстрый sanity-check) -----------------
if gzip -t "$DUMP" 2>>"$LOG"; then
  log "OK  gzip-целостность подтверждена"
else
  log "ERR архив повреждён!"
  exit 1
fi

# --- 5) ротация --------------------------------------------------------------
find "$BACKUP_DIR" -maxdepth 1 -name 'schoolhub-*.sql.gz'     -mtime +"$KEEP_DAYS"  -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'schoolhub-*.sql.gz.sha256' -mtime +"$KEEP_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'weekly-schoolhub-*.sql.gz' -mtime +"$((KEEP_WEEKLY * 7))" -delete
log "OK  ротация: daily=${KEEP_DAYS}d weekly=${KEEP_WEEKLY}w"

# --- 6) бэкап загруженных документов приказов -------------------------------
# Файлы хранятся в $DATA_ROOT/uploads — их копирует Hyper Backup вместе с папкой.
# Здесь лишь фиксируем объём, чтобы рост был виден в логе.
if [ -d "$DATA_ROOT/uploads" ]; then
  log "INFO uploads: $(du -sh "$DATA_ROOT/uploads" | cut -f1)"
fi

log "=== backup done ==="
