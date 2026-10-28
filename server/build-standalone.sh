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
"$PY" -m PyInstaller --noconfirm --clean --onefile --name onec-agent-server \
  --distpath ../dist --workpath ../dist/build --specpath ../dist/build \
  --add-data "$PWD/onec_agent/web:onec_agent/web" \
  --add-data "$PWD/$STAGE/evals:evals" \
  --add-data "$PWD/notes:notes" \
  --hidden-import truststore \
  onec_agent_server.py
ls -la ../dist/onec-agent-server*
