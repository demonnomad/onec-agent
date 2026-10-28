"""Встроенная модель: настройки, состояние для панели, API панели."""
import asyncio

import aiohttp
import pytest
from aiohttp import web

from onec_agent import app as app_module
from onec_agent import local_model


def test_settings_defaults_and_validation():
    assert local_model.settings()["ctx"] == 32768 and local_model.settings()["parallel"] == 1
    saved = local_model.save_settings({"ctx": "16384", "autostart": False})
    assert saved["ctx"] == 16384 and local_model.settings()["autostart"] is False
    for bad in ({"ctx": 100}, {"parallel": 0}, {"preset": "нет-такой"}, {"лишнее": 1}):
        with pytest.raises(local_model.LocalModelError):
            local_model.save_settings(bad)


def test_status_lists_presets_not_downloaded():
    status = local_model.Manager().status()
    assert status["state"] == "stopped" and status["memory_mb"] == 0
    assert {p["id"] for p in status["presets"]} == set(local_model.PRESETS)
    assert not any(p["downloaded"] for p in status["presets"])


def test_start_without_model_file_explains(monkeypatch):
    monkeypatch.setattr(local_model, "find_server_binary", lambda: "/bin/echo")
    with pytest.raises(local_model.LocalModelError, match="не скачана"):
        asyncio.run(local_model.Manager().start("qwen3-4b"))


def test_panel_api_needs_token_and_saves_settings():
    async def scenario():
        runner = web.AppRunner(app_module.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}/panel/api"
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(base + "/status") as r:
                    denied = r.status
                headers = {"X-Agent-Token": "тестовый-токен"}
                async with http.post(base + "/model/settings", json={"idle_unload_min": 5}, headers=headers) as r:
                    saved = await r.json()
                async with http.post(base + "/model/settings", json={"ctx": 1}, headers=headers) as r:
                    bad = await r.json()
            return denied, saved, bad
        finally:
            await runner.cleanup()

    denied, saved, bad = asyncio.run(scenario())
    assert denied == 401
    assert saved["ok"] and saved["settings"]["idle_unload_min"] == 5
    assert not bad["ok"] and "Контекст" in bad["error"]


def test_bases_list_add_and_ibases(tmp_path, monkeypatch):
    from onec_agent import bases
    db = tmp_path / "Демо"
    db.mkdir()
    (db / "1Cv8.1CD").write_bytes(b"")
    ib = tmp_path / "ibases.v8i"
    ib.write_text('[Моя база]\nConnect=File="%s";\n[Сервер]\nConnect=Srvr="x";Ref="y";\n' % db, "utf-8")
    monkeypatch.setattr(bases, "IBASES_FILES", [ib])
    listed = bases.list_bases()
    assert [b["title"] for b in listed] == ["Моя база"] and listed[0]["exists"]
    with pytest.raises(bases.BaseError, match="1Cv8.1CD"):
        bases.add_base(str(tmp_path))
    added = bases.add_base(str(db), "Демо вручную", "Админ")
    assert added["title"] == "Демо вручную" and added["user"] == "Админ"
    assert len(bases.list_bases()) == 1  # тот же путь из ibases.v8i не дублируется
