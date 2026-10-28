#!/usr/bin/env bash
# Загрузка расширения из XML-исходников в файловую базу и проверка модулей.
#   ./deploy.sh <каталог базы> [пользователь]
# Пример: ./deploy.sh ~/bases/ДемоБП Администратор
# Откат: ibcmd infobase config extension delete --db-path=<база> --user=<польз> --name=иик_ИИКонсультант
set -euo pipefail

IB="${1:?укажи каталог файловой базы}"
USER_1C="${2:-}"
EXT=иик_ИИКонсультант
PLATFORM="${PLATFORM_1C:-/opt/1cv8/8.3.27.2074}"
SRC="$(cd "$(dirname "$0")/src" && pwd)"
LOG="$(mktemp -t onec-agent-check).log"
AUTH=(); [ -n "$USER_1C" ] && AUTH=(--user="$USER_1C")
DAUTH=(); [ -n "$USER_1C" ] && DAUTH=(/N"$USER_1C")

echo "→ импорт $SRC"
"$PLATFORM/ibcmd" infobase config import --db-path="$IB" "${AUTH[@]}" --extension="$EXT" "$SRC"
echo "→ проверка метаданных"
"$PLATFORM/ibcmd" infobase config check --db-path="$IB" "${AUTH[@]}" --extension="$EXT"
echo "→ проверка модулей (тонкий, веб-клиент, сервер)"
if ! "$PLATFORM/1cv8" DESIGNER /F"$IB" "${DAUTH[@]}" /CheckModules -ThinClient -WebClient -Server -Extension "$EXT" \
    /DisableStartupDialogs /DisableStartupMessages /Out"$LOG"; then
  cat "$LOG"; echo "✗ ошибки в модулях — расширение загружено, но НЕ применено"; exit 1
fi
echo "→ применение"
"$PLATFORM/ibcmd" infobase config apply --db-path="$IB" "${AUTH[@]}" --extension="$EXT" --dynamic=disable --force
echo "✓ готово"
