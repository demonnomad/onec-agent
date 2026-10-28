"""WebSocket сервера: приветствие, модель базы, права на настройки."""
import asyncio

import aiohttp
from aiohttp import web

from onec_agent import app as app_module


async def _with_server(scenario):
    application = app_module.make_app() if hasattr(app_module, "make_app") else None
    assert application is not None, "нужна фабрика make_app() для тестов"
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                return await scenario(ws, http, port)
    finally:
        await runner.cleanup()


async def _until(ws, kind):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 5)
        if msg["type"] == kind:
            return msg


def test_hello_reports_model_and_rights():
    async def scenario(ws, http, port):
        await _until(ws, "ready")
        await ws.send_json({"type": "hello", "session": "s" * 20, "token": "тестовый-токен",
                            "context": {"config_name": "БП", "base_id": "b1", "is_admin": False}})
        model = await _until(ws, "model")
        await ws.send_json({"type": "settings_save", "profile": {"provider": "local", "model": "m"}})
        denied = await _until(ws, "settings_error")
        async with http.get(f"http://127.0.0.1:{port}/health") as r:
            health = await r.json()
        return model, denied, health

    model, denied, health = asyncio.run(_with_server(scenario))
    assert model["label"].startswith("свой сервер") and model["can_edit"] is False
    assert "администратор" in denied["message"]
    assert health["ok"] is True


def test_admin_saves_local_profile():
    async def scenario(ws, http, port):
        await _until(ws, "ready")
        await ws.send_json({"type": "hello", "session": "s" * 20, "token": "тестовый-токен",
                            "context": {"config_name": "БП", "base_id": "b2", "is_admin": True}})
        await _until(ws, "model")
        await ws.send_json({"type": "settings_save",
                            "profile": {"provider": "local", "base_url": "http://127.0.0.1:1/v1", "model": "другая"}})
        return await _until(ws, "settings_saved")

    saved = asyncio.run(_with_server(scenario))
    assert saved["profile"]["model"] == "другая" and not saved["profile"]["cloud"]


def test_ws_and_upload_require_token():
    async def scenario(ws, http, port):
        await _until(ws, "ready")
        await ws.send_json({"type": "settings_get"})  # до hello с токеном — отказ
        before_hello = await _until(ws, "auth_error")
        results = [before_hello]
        for token in ("не тот", "тестовый-токен"):
            async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as other:
                await _until(other, "ready")
                await other.send_json({"type": "hello", "token": token,
                                       "context": {"config_name": "БП", "base_id": "b3", "token": token}})
                results.append(await asyncio.wait_for(other.receive_json(), 5))
        form = aiohttp.FormData()
        form.add_field("session", "s" * 20)
        form.add_field("file", b"x", filename="a.txt")
        async with http.post(f"http://127.0.0.1:{port}/upload", data=form) as r:
            results.append(r.status)
        return results

    before_hello, wrong, right, upload_status = asyncio.run(_with_server(scenario))
    assert before_hello["type"] == "auth_error" and wrong["type"] == "auth_error"
    assert right["type"] == "model" and upload_status == 401


def test_token_file_created_once(tmp_path, monkeypatch):
    import os
    from onec_agent import auth, config
    monkeypatch.setattr(config, "TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_FILE", str(tmp_path / "data" / "token"))
    first = auth.token()
    assert len(first) >= 24 and auth.token() == first
    assert oct(os.stat(tmp_path / "data" / "token").st_mode)[-3:] == "600"
    assert auth.check(first) and not auth.check("") and not auth.check(None)
