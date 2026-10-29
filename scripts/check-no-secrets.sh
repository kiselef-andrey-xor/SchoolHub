#!/usr/bin/env bash
# ============================================================================
#  Проверка «в файлах нет захардкоженных секретов» (review/REVIEW.md P0-3).
#
#  Тот же класс дефекта, что привёл к утечке SECRET_KEY и пароля Владельца
#  в публичный репозиторий: значение записано литералом в compose-файле,
#  а не взято из окружения (`${SECRET_KEY}`) или из `.env` на сервере.
#
#  Использование:
#      bash scripts/check-no-secrets.sh                # конфигурационные файлы из индекса git
#      bash scripts/check-no-secrets.sh --all-tracked  # вообще все tracked-файлы (CI)
#      bash scripts/check-no-secrets.sh a.yml b.env    # только указанные (pre-commit)
#
#  Код возврата: 0 — чисто, 1 — найдены секреты.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

# SECRET_KEY / пароли, заданные ЗНАЧЕНИЕМ. Ссылки на окружение (${VAR}, ${VAR:-...})
# и короткие значения (< 16 символов) легитимны и отсекаются.
PATTERN='^[[:space:]]*(SECRET_KEY|OWNER_PASSWORD|POSTGRES_PASSWORD|LDAP_BIND_PASSWORD|DB_PASSWORD):[[:space:]]*"?[A-Za-z0-9#!=@._/-]{16,}"?[[:space:]]*$'

if [[ "${1:-}" == "--all-tracked" ]]; then
  # режим CI: проверяем весь индекс, а не только конфигурационные файлы
  mapfile -t FILES < <(git ls-files)
elif [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  # режим разработчика/хука: секрет может приехать только из конфигов и скриптов
  mapfile -t FILES < <(git ls-files -- '*.yml' '*.yaml' '*.env' '.env.example' '*.conf' '*.ini' '*.sh' '*.ps1' '*.template')
fi

FOUND=0
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || continue
  # grep -vE '\$\{' — оставляем только строки без подстановки окружения
  if grep -HnE "$PATTERN" "$f" | grep -vE '\$\{'; then
    FOUND=1
  fi
done

if [[ $FOUND -ne 0 ]]; then
  echo "✗ Найдены захардкоженные секреты: вынесите значение в .env на сервере"
  echo "  и оставьте в файле ссылку вида \${SECRET_KEY} (см. .env.example)."
  exit 1
fi

echo "✓ секретов в проверяемых файлах нет"
