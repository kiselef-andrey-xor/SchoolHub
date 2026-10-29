#!/bin/bash
# ============================================================================
#  SchoolHub — установка на Synology одной командой
#
#    sudo bash install-nas.sh
#
#  Что делает:
#    1. проверяет Docker и свободное место;
#    2. копирует проект в /volume1/docker/schoolhub/app (если запускается из
#       другого места, например из распакованной папки в /volume1/homes/...);
#    3. собирает образы и запускает стек;
#    4. дожидается готовности и ПЕЧАТАЕТ код первоначальной настройки.
#
#  После этого: откройте http://<адрес-NAS>:8080 и создайте Владельца.
# ============================================================================
set -euo pipefail

TARGET="${SCHOOLHUB_DIR:-/volume1/docker/schoolhub}"
APP_DIR="$TARGET/app"
PORT="${HTTP_PORT:-8080}"
COMPOSE="$APP_DIR/deploy/synology/docker-compose.yml"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
step()  { printf '\n\033[36m==> %s\033[0m\n' "$1"; }

# --- 0. права и docker -------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  red "Запустите с sudo:  sudo bash install-nas.sh"
  exit 1
fi

step "Проверка окружения"
if ! command -v docker >/dev/null 2>&1; then
  red "Docker не найден. Установите «Container Manager» в Центре пакетов DSM и повторите."
  exit 1
fi
green "Docker: $(docker --version | head -1)"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  red "Не найден docker compose. Обновите Container Manager в Центре пакетов."
  exit 1
fi
green "Compose: $($DC version --short 2>/dev/null || echo ok)"

ARCH=$(uname -m)
yellow "Архитектура: $ARCH  (DS223 = aarch64, образы мультиархитектурные)"

FREE=$(df -m /volume1 2>/dev/null | awk 'NR==2{print $4}')
[ -n "${FREE:-}" ] && [ "$FREE" -lt 1500 ] && yellow "Внимание: свободно всего ${FREE} МБ на /volume1"

# --- 1. расположение проекта -------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
  step "Копирование проекта в $APP_DIR"
  mkdir -p "$APP_DIR"
  # node_modules/dist не копируем: они будут созданы внутри контейнера при сборке
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude node_modules --exclude dist --exclude __pycache__ \
          --exclude .pytest_cache --exclude 'storage/uploads/*' "$SCRIPT_DIR"/ "$APP_DIR"/
  else
    cp -R "$SCRIPT_DIR"/. "$APP_DIR"/
    rm -rf "$APP_DIR/frontend/node_modules" "$APP_DIR/frontend/dist"
  fi
  green "Скопировано"
else
  step "Проект уже на месте: $APP_DIR"
fi

if [ ! -f "$COMPOSE" ]; then
  red "Не найден $COMPOSE — установка прервана"
  exit 1
fi

# --- 2. .env (необязательный) ------------------------------------------------
if [ ! -f "$APP_DIR/deploy/synology/.env" ] && [ -f "$APP_DIR/deploy/synology/.env.nas.example" ]; then
  cp "$APP_DIR/deploy/synology/.env.nas.example" "$APP_DIR/deploy/synology/.env"
  chmod 600 "$APP_DIR/deploy/synology/.env"
  yellow "Создан .env со значениями по умолчанию (можно отредактировать позже)"
fi

# --- 3. сборка и запуск ------------------------------------------------------
step "Сборка и запуск (первый раз на DS223: 5–12 минут)"
cd "$APP_DIR"
$DC -f "$COMPOSE" --env-file "$APP_DIR/deploy/synology/.env" up -d --build

step "Ожидание готовности"
for i in $(seq 1 60); do
  if curl -fsS "http://localhost:${PORT}/api/v1/health" >/dev/null 2>&1; then
    green "Сервис отвечает (попытка $i)"
    break
  fi
  [ "$i" = 60 ] && { red "Сервис не ответил за 5 минут. Смотрите журнал: docker logs schoolhub-backend"; exit 1; }
  sleep 5
done

# --- 4. код первоначальной настройки ----------------------------------------
step "Первоначальная настройка"
$DC -f "$COMPOSE" ps

TOKEN=$(docker logs schoolhub-backend 2>&1 | grep -oE "Код первоначальной настройки: [A-Z0-9]{6,12}" | tail -1 | awk '{print $NF}')
IP=$(ip route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)

echo
if [ -n "${TOKEN:-}" ]; then
  green "┌──────────────────────────────────────────────────────────────┐"
  green "│  Код первоначальной настройки:  ${TOKEN}                        │"
  green "└──────────────────────────────────────────────────────────────┘"
else
  yellow "Код настройки не найден в журнале (возможно, система уже настроена)."
  yellow "Получить заново:  docker exec -it schoolhub-backend python -m scripts.first_owner --show-token"
fi

echo
green "Откройте в браузере:   http://${IP:-<адрес-NAS>}:${PORT}"
echo   "  1. Введите код настройки"
echo   "  2. Создайте учётную запись Владельца (e-mail + пароль)"
echo   "  3. Войдите и добавьте сотрудников в разделе «Пользователи»"
echo
echo   "Полезные команды:"
echo   "  журнал:      docker logs -f schoolhub-backend"
echo   " диагностика: sudo bash $APP_DIR/deploy/synology/check-health.sh"
echo   " остановка:   cd $APP_DIR && sudo $DC -f deploy/synology/docker-compose.yml down"
echo
