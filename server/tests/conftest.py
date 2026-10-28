"""Общие фикстуры: всё, что сервер пишет на диск (журнал, профили моделей, база знаний, выгрузки),
уходит во временную папку теста. Ни 1С, ни LM Studio, ни выгрузка БП для тестов не нужны."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onec_agent import audit, config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_LOG", str(tmp_path / "logs" / "audit.jsonl"))
    monkeypatch.setattr(audit, "LOG", tmp_path / "logs" / "audit.jsonl")
    monkeypatch.setattr(config, "MODELS_FILE", str(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(config, "KB_DIR", str(tmp_path / "kb"))
    monkeypatch.setattr(config, "SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "TOKEN", "тестовый-токен")
    monkeypatch.setattr(config, "TOKEN_FILE", str(tmp_path / "data" / "token"))
    return tmp_path


@pytest.fixture
def session():
    """Сессия агента без транспорта: emit копит события, LLM не вызывается."""
    from onec_agent.agent import AgentSession

    events = []

    async def emit(event):
        events.append(event)

    s = AgentSession(emit, None)
    s.context = {"config_name": "БухгалтерияПредприятия", "user": "Тест", "is_admin": True}
    s.reset()
    s.events = events
    return s
