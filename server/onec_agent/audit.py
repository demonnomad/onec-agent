"""Журнал действий агента: какой инструмент, с какими аргументами, чем кончилось.

Пишем только аргументы и сводку — сами данные из базы в журнал не попадают.
"""
import datetime as dt
import json
from pathlib import Path

from . import config

LOG = Path(config.AUDIT_LOG)


def record(context: dict, tool: str, args: dict, payload: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "user": context.get("user"),
        "config": context.get("config_name"),
        "tool": tool,
        "args": args,
        "ok": bool(payload.get("ok")),
        "summary": payload.get("summary") if payload.get("ok") else payload.get("error"),
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
