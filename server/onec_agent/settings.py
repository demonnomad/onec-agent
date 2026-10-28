"""Профили модели по базам: {base_id: profile}. Файл data/models.json, права 600.

Ключ API хранится только здесь: в интерфейс уходит признак has_key, а сохранение без нового ключа
оставляет прежний. Менять профиль может только администратор базы (is_admin из контекста 1С).
"""
import datetime as dt
import json
import os
from pathlib import Path

from . import config, providers

FIELDS = ("provider", "base_url", "model", "scope", "auth_url", "verify_ssl")


def _path() -> Path:
    return Path(config.MODELS_FILE)


def _load_all() -> dict:
    path = _path()
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


def _save_all(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def profile_for(base_id: str) -> dict:
    """Действующий профиль базы (с ключом — только для сервера)."""
    return _load_all().get(base_id) or providers.default_profile()


def public(profile: dict) -> dict:
    """Профиль для интерфейса: без ключа."""
    shown = {k: profile.get(k) for k in FIELDS if profile.get(k) is not None}
    shown["has_key"] = bool(profile.get("api_key"))
    shown["cloud"] = providers.is_cloud(profile)
    shown["label"] = providers.label(profile)
    for k in ("consent_by", "consent_at"):
        if profile.get(k):
            shown[k] = profile[k]
    return shown


def merge(base_id: str, incoming: dict, user: str, for_test: bool = False) -> dict:
    """Новый профиль из формы поверх сохранённого: пустой ключ не затирает прежний.
    Для облачного провайдера обязательно согласие на передачу данных."""
    current = profile_for(base_id)
    provider = incoming.get("provider")
    if provider not in providers.PROVIDERS:
        raise ValueError("Неизвестный источник модели")
    info = providers.PROVIDERS[provider]
    profile = {k: incoming[k] for k in FIELDS if k in incoming and incoming[k] not in (None, "")}
    profile["provider"] = provider
    profile.setdefault("base_url", info["base_url"])
    profile.setdefault("model", info["model"])
    if not profile["model"]:
        raise ValueError("Укажите модель")
    key = (incoming.get("api_key") or "").strip()
    same_provider = current.get("provider") == provider
    if key:
        profile["api_key"] = key
    elif same_provider and current.get("api_key"):
        profile["api_key"] = current["api_key"]
    if info["needs_key"] and not profile.get("api_key"):
        raise ValueError("Для этого провайдера нужен ключ API")
    if info["cloud"] and not for_test:  # проверка шлёт только «ответь: готово», без данных базы
        if not incoming.get("cloud_consent"):
            raise ValueError("Облачный провайдер: нужно согласие на передачу данных базы")
        profile["consent_by"] = user
        profile["consent_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return profile


def save(base_id: str, profile: dict) -> None:
    data = _load_all()
    data[base_id] = profile
    _save_all(data)
