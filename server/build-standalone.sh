#!/usr/bin/env bash
# Сборка сервера одним исполняемым файлом (PyInstaller): dist/onec-agent-server (на Windows — .exe).
# Внутрь упаковываются web/, evals/, notes/; данные (data/, logs/) создаются рядом с файлом при запуске.
#   ./build-standalone.sh          → ../dist/onec-agent-server[.exe]
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-../.venv/bin/python}"
[ -x "$PY" ] || PY=python3
# Наборы тестов — без локальных (*.local.json: наборы конкретных клиентских баз в сборку не попадают).
STAGE=../dist/build/stage; rm -rf "$STAGE"; mkdir -p "$STAGE/evals"
for f in evals/*.json; do case "$f" in *.local.json) ;; *) cp "$f" "$STAGE/evals/";; esac; done
# Разделитель --add-data: «;» на Windows, «:» на остальных; пути относительные — Git Bash на Windows
# отдаёт $PWD в виде /d/a/…, который PyInstaller под Windows не понимает.
SEP=":"; case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) SEP=";";; esac
"$PY" -m PyInstaller --noconfirm --clean --onefile --name onec-agent-server \
  --distpath ../dist --workpath ../dist/build --specpath ../dist/build \
  --add-data "onec_agent/web${SEP}onec_agent/web" \
  --add-data "$STAGE/evals${SEP}evals" \
  --add-data "notes${SEP}notes" \
  --hidden-import truststore \
  onec_agent_server.py
ls -la ../dist/onec-agent-server*
