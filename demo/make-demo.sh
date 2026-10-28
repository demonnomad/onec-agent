#!/usr/bin/env bash
# Демо-база БП 3.0 для тестов консультанта: восстановить чистую базу (после начального заполнения,
# с расширением) и заполнить вымышленными данными.
#   ./make-demo.sh <каталог базы> <clean.dt>
set -euo pipefail
IB="${1:?каталог файловой базы}"; CLEAN="${2:?чистая выгрузка .dt}"
PLATFORM="${PLATFORM_1C:-/opt/1cv8/8.3.27.2074}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$(mktemp -t demo-fill).log"
"$PLATFORM/ibcmd" infobase restore --db-path="$IB" "$CLEAN" --force < /dev/null | tail -1
"$PLATFORM/1cv8" ENTERPRISE /F"$IB" /DisableStartupDialogs /DisableStartupMessages \
  /Execute"$HERE/ДемоДанные.epf" /C"$LOG"
cat "$LOG"
! grep -q "^ОШИБКА" "$LOG"
