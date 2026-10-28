#!/usr/bin/env bash
# Исходники конфигурации для code_read / code_search: тексты модулей из выгрузки в файлы
# кладутся в регистр иик_ИсходныйКод расширения (в саму базу; в репозиторий код 1С не попадает).
#
#   ./load-sources.sh <база> [пользователь] [Источник=каталог ...]
#
# Без каталогов — выгружает сам основную конфигурацию и расширения базы (ibcmd config export, нужна
# монопольная работа: опубликованную через ibsrv базу на время выгрузки остановите; приложение
# onec-agent делает это само). Каталоги можно задать и явно: Расширение1=<каталог выгрузки расширения>.
# Пример: ./load-sources.sh ~/bases/ДемоБП
#         ./load-sources.sh ~/bases/Клиент Администратор Конфигурация=~/src/bp Расширение1=~/src/ext1
set -euo pipefail

IB="${1:?укажи каталог файловой базы}"; shift
USER_1C=""
if [ $# -gt 0 ] && [[ "$1" != *=* ]]; then USER_1C="$1"; shift; fi
PLATFORM="${PLATFORM_1C:-/opt/1cv8/8.3.27.2074}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d -t onec-agent-sources)"
trap 'rm -rf "$WORK"' EXIT
AUTH=(); [ -n "$USER_1C" ] && AUTH=(--user="$USER_1C")
DAUTH=(); [ -n "$USER_1C" ] && DAUTH=(/N"$USER_1C")

SPECS=("$@")
if [ ${#SPECS[@]} -eq 0 ]; then
  echo "→ выгрузка конфигурации базы в файлы"
  "$PLATFORM/ibcmd" infobase config export --db-path="$IB" "${AUTH[@]}" "$WORK/config" < /dev/null | tail -1
  SPECS=("Конфигурация=$WORK/config")
  # Расширения базы (доработки клиента) — отдельными источниками; сам консультант не нужен.
  echo "→ выгрузка расширений базы"
  "$PLATFORM/ibcmd" infobase config export all-extensions --db-path="$IB" "${AUTH[@]}" "$WORK/ext" < /dev/null | tail -1
  for dir in "$WORK"/ext/*/; do
    [ -d "$dir" ] || continue
    ext="$(basename "$dir")"
    [ "$ext" = "иик_ИИКонсультант" ] && continue
    SPECS+=("$ext=$dir")
  done
fi

echo "→ сборка обработки загрузки"
"$PLATFORM/ibcmd" infobase create --db-path="$WORK/ctx" --create-database < /dev/null > /dev/null
"$PLATFORM/1cv8" DESIGNER /F"$WORK/ctx" /DisableStartupDialogs /DisableStartupMessages /Out"$WORK/build.log" \
  /LoadExternalDataProcessorOrReportFromFiles "$HERE/sources-loader/ЗагрузкаИсходников.xml" "$WORK/loader.epf" || true
[ -f "$WORK/loader.epf" ] || { cat "$WORK/build.log"; echo "✗ обработка загрузки не собралась"; exit 1; }

PARAM="$WORK/load.log"
for spec in "${SPECS[@]}"; do
  name="${spec%%=*}"; dir="${spec#*=}"; dir="${dir/#\~/$HOME}"
  PARAM="$PARAM|$name=$(cd "$dir" && pwd)"
done
echo "→ загрузка модулей в базу"
"$PLATFORM/1cv8" ENTERPRISE /F"$IB" "${DAUTH[@]}" /DisableStartupDialogs /DisableStartupMessages \
  /Execute"$WORK/loader.epf" /C"$PARAM"
cat "$WORK/load.log"; echo
! grep -q "^ОШИБКА" "$WORK/load.log"
