"""Токен доступа к серверу агента.

Без токена страница чата не получит сессию, а /upload не примет файл: иначе любой, кто достучится
до порта (в том числе чужая веб-страница в браузере пользователя — WebSocket не ограничен CORS),
мог бы говорить с моделью от имени базы и менять её настройки.

Токен берётся из ONEC_AGENT_TOKEN, иначе из файла (по умолчанию server/data/token); если файла нет,
он создаётся при первом запуске (права 600). В 1С токен вводится один раз: «Адрес сервера…».
"""
import hmac
import logging
import os
import secrets
from pathlib import Path

from . import config

log = logging.getLogger("onec_agent")


def token() -> str:
    if config.TOKEN:
        return config.TOKEN
    path = Path(config.TOKEN_FILE)
    if path.is_file():
        value = path.read_text("utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    log.info("создан токен доступа: %s (введите его в 1С: «Адрес сервера…»)", path.resolve())
    return value


def check(candidate) -> bool:
    return isinstance(candidate, str) and bool(candidate) and hmac.compare_digest(candidate.encode(), token().encode())
