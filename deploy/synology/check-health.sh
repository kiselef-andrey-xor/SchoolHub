#!/bin/bash
# ============================================================================
#  Диагностика SchoolHub на Synology: контейнеры, БД, LDAP/AD, место, память.
#  Запуск:  sudo bash /volume1/docker/schoolhub/check-health.sh
# ============================================================================
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/volume1/docker/schoolhub}"
[ -f "$DATA_ROOT/.env" ] && { set -a; . "$DATA_ROOT/.env"; set +a; }

HTTP_PORT="${HTTP_PORT:-8080}"
OK="\033[32m[OK]\033[0m"; WARN="\033[33m[!!]\033[0m"; ERR="\033[31m[X ]\033[0m"
say()  { echo -e "$1 $2"; }
head1(){ echo -e "\n\033[36m=== $1 ===\033[0m"; }

head1 "1. Контейнеры"
docker ps -a --filter "name=schoolhub" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
for c in schoolhub-db schoolhub-backend schoolhub-frontend; do
  state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "absent")
  if [ "$state" = "running" ]; then say "$OK" "$c запущен"
  else say "$ERR" "$c: $state"; fi
done

head1 "2. Ресурсы NAS (DS223: 2 ГБ RAM)"
free -m | awk 'NR==1||/Mem:/'
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' 2>/dev/null | grep -E 'schoolhub|NAME' || true
USED=$(df -h /volume1 | awk 'NR==2{print $5}')
say "$OK" "Занято на /volume1: $USED"

head1 "3. API"
if curl -fsS "http://localhost:${HTTP_PORT}/api/v1/health" >/dev/null 2>&1; then
  say "$OK" "health через nginx: $(curl -fsS "http://localhost:${HTTP_PORT}/api/v1/health")"
else
  say "$ERR" "нет ответа на http://localhost:${HTTP_PORT}/api/v1/health"
  docker logs --tail 30 schoolhub-backend 2>&1 | sed 's/^/      /'
fi

head1 "4. База данных"
if docker exec schoolhub-db pg_isready -U "${POSTGRES_USER:-schoolhub}" >/dev/null 2>&1; then
  say "$OK" "PostgreSQL принимает подключения"
  docker exec schoolhub-db psql -U "${POSTGRES_USER:-schoolhub}" -d "${POSTGRES_DB:-schoolhub}" -tAc \
    "select 'пользователей: '||count(*) from users union all
     select '  из домена: '||count(*) from users where auth_source='ldap' union all
     select 'приказов:    '||count(*) from orders union all
     select 'задач:       '||count(*) from tasks union all
     select 'записей аудита: '||count(*) from audit_log;" 2>/dev/null | sed 's/^/      /'
else
  say "$ERR" "PostgreSQL недоступен"
  docker logs --tail 20 schoolhub-db 2>&1 | sed 's/^/      /'
fi

head1 "5. Active Directory / LDAP"
if [ "${LDAP_ENABLED:-false}" != "true" ]; then
  say "$WARN" "LDAP_ENABLED != true — вход только по локальным учёткам"
else
  HOST_PORT="${LDAP_SERVER:-}"; HOST="${HOST_PORT%%:*}"; PORT="${HOST_PORT##*:}"
  if [ -z "$HOST" ] || [ "$HOST" = "$PORT" ]; then
    say "$ERR" "LDAP_SERVER не задан (ожидается dc01.school.local:636)"
  else
    if docker exec schoolhub-backend python -c "import socket,sys;socket.create_connection(('$HOST',$PORT),5)" 2>/dev/null; then
      say "$OK" "порт $HOST:$PORT доступен из контейнера backend"
    else
      say "$ERR" "НЕ удается подключиться к $HOST:$PORT из контейнера"
      echo "      причины: DNS внутри контейнера, firewall DSM/DC, неверный порт"
      echo "      проверка DNS: docker exec schoolhub-backend getent hosts $HOST"
    fi
    if [ -f "${DATA_ROOT}/certs/rootca.pem" ]; then
      say "$OK" "rootca.pem найден (${LDAP_CA_CERT:-/certs/rootca.pem})"
      docker exec schoolhub-backend python - "$HOST" "$PORT" <<'PY' 2>/dev/null | sed 's/^/      /' || true
import socket, ssl, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    ctx = ssl.create_default_context(cafile="/certs/rootca.pem")
    with socket.create_connection((host, port), 5) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
        cert = ss.getpeercert()
        print("TLS OK, subject:", dict(x[0] for x in cert.get("subject", ())), "до", cert.get("notAfter"))
except Exception as e:
    print("TLS FAIL:", type(e).__name__, e)
PY
    else
      say "$WARN" "${DATA_ROOT}/certs/rootca.pem отсутствует — при самоподписанном CA будет ошибка TLS"
    fi
  fi
fi

head1 "6. Права на каталоги данных"
for d in pgdata uploads certs; do
  p="$DATA_ROOT/$d"
  if [ -d "$p" ]; then say "$OK" "$p: $(stat -c '%U:%G %a' "$p")"
  else say "$WARN" "$p отсутствует (создастся при первом старте, кроме certs)"; fi
done
[ -f "$DATA_ROOT/.env" ] && say "$OK" ".env: $(stat -c '%a' "$DATA_ROOT/.env") (ожидаем 600)"

head1 "7. Последние события приложения"
docker logs --tail 15 schoolhub-backend 2>&1 | sed 's/^/      /'

echo
say "$OK" "Диагностика завершена. При ошибках смотрите: docker logs schoolhub-backend"
