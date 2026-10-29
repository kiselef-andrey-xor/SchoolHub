#!/usr/bin/env bash
# ============================================================================
#  Проверка «артефакты сборки не попали под контроль версий» (review §6.1).
#
#  `frontend/dist` и `frontend/tsconfig.tsbuildinfo` описаны в `.gitignore`, но
#  были закоммичены до его появления и потому продолжали отслеживаться git'ом
#  (dist — ещё и дубликат vendor-бандла на 375 КБ в каждом клоне).
#
#  Скрипт проверяет ДВЕ ситуации и используется и git-хуком, и CI — чтобы правило
#  не разъезжалось в двух местах:
#    1. артефакт уже отслеживается git'ом (нужен `git rm --cached`);
#    2. артефакт добавлен в текущий индекс (хук ловит это до коммита).
#
#  Код возврата: 0 — чисто, 1 — найдены артефакты.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PATTERN='^frontend/dist/|tsconfig\.tsbuildinfo$'
CURE='git rm -r --cached frontend/dist frontend/tsconfig.tsbuildinfo'
BAD=0

TRACKED="$(git ls-files | grep -E "$PATTERN" || true)"
if [[ -n "$TRACKED" ]]; then
  echo "✗ git отслеживает артефакты сборки фронтенда:"
  echo "$TRACKED" | sed 's/^/    /'
  echo "  лечение: $CURE"
  BAD=1
fi

# --diff-filter=ACMR: только добавленные/изменённые. Без фильтра скрипт ругался бы
# и на удаление артефактов из-под контроля версий (а это как раз то, что нужно).
STAGED="$(git diff --cached --name-only --diff-filter=ACMR | grep -E "$PATTERN" || true)"
if [[ -n "$STAGED" ]]; then
  echo "✗ в индекс добавлены артефакты сборки фронтенда:"
  echo "$STAGED" | sed 's/^/    /'
  echo "  лечение: git reset -- $STAGED"
  BAD=1
fi

[[ $BAD -eq 0 ]] || exit 1
echo "✓ артефактов сборки под контролем версий нет"
