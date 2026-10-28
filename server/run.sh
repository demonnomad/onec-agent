#!/usr/bin/env bash
# Запуск сервера фундамента: ./run.sh  (переменные ONEC_AGENT_* см. onec_agent/config.py)
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1  # лог сразу на диск — иначе при разборе зависаний последние записи не видны
exec ../.venv/bin/python -m onec_agent
