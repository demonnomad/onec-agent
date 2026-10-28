"""Профили модели: правила сохранения, ключи, согласие на облако; адаптеры провайдеров."""
import asyncio
import json
import os
import sys

import aiohttp
import pytest
from aiohttp import web

from onec_agent import config, providers, settings


def test_cloud_requires_key_and_consent():
    with pytest.raises(ValueError, match="ключ"):
        settings.merge("b", {"provider": "anthropic"}, "Админ")
    with pytest.raises(ValueError, match="согласие"):
        settings.merge("b", {"provider": "anthropic", "api_key": "k"}, "Админ")
    p = settings.merge("b", {"provider": "anthropic", "api_key": "k", "cloud_consent": True}, "Админ")
    assert p["consent_by"] == "Админ" and p["consent_at"]


def test_connection_test_needs_no_consent():
    assert settings.merge("b", {"provider": "openai", "api_key": "k", "model": "m"}, "А", for_test=True)["provider"] == "openai"


def test_empty_key_keeps_previous_and_is_not_shown():
    settings.save("b", settings.merge("b", {"provider": "anthropic", "api_key": "sk-1", "cloud_consent": True}, "А"))
    again = settings.merge("b", {"provider": "anthropic", "api_key": "", "cloud_consent": True}, "А")
    assert again["api_key"] == "sk-1"
    shown = settings.public(again)
    assert "api_key" not in shown and shown["has_key"] and shown["cloud"]
    if sys.platform != "win32":  # на Windows POSIX-прав нет
        assert oct(os.stat(config.MODELS_FILE).st_mode & 0o777) == "0o600"


def test_default_profile_is_local():
    p = settings.profile_for("нет-такой-базы")
    assert p["provider"] == "local" and not providers.is_cloud(p)
    assert providers.label(p).startswith("свой сервер")


HISTORY = [
    {"role": "system", "content": "сис"},
    {"role": "user", "content": "Сколько в кассе?"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "register_balance", "arguments": "{\"register\": \"X\"}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ИТОГ…\n{\"rows\": []}"},
]
TOOLS = [{"type": "function", "function": {"name": "register_balance", "description": "d", "parameters": {"type": "object"}}}]


def test_history_to_anthropic():
    system, msgs = providers._to_anthropic(HISTORY)
    assert system == "сис"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "register_balance", "input": {"register": "X"}}
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[2]["content"][0]["tool_use_id"] == "c1"


def test_history_to_gigachat():
    msgs = providers._to_gigachat(HISTORY)
    assert msgs[2]["function_call"] == {"name": "register_balance", "arguments": {"register": "X"}}
    assert msgs[3]["role"] == "function" and msgs[3]["name"] == "register_balance"
    assert json.loads(msgs[3]["content"])["result"].startswith("ИТОГ")  # не-JSON результат обёрнут


def _fake_server():
    seen = {}

    async def anthropic(req):
        seen["anthropic"] = (req.headers.get("x-api-key"), await req.json())
        resp = web.StreamResponse()
        await resp.prepare(req)
        for e in [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Смотрю"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "register_balance"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"register\": "}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"Р\"}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]:
            await resp.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
        return resp

    async def oauth(req):
        seen["oauth"] = (req.headers.get("Authorization"), dict(await req.post()))
        return web.json_response({"access_token": "tok", "expires_at": 9999999999999})

    async def gigachat(req):
        seen["gigachat"] = (req.headers.get("Authorization"), await req.json())
        return web.json_response({"choices": [{"finish_reason": "function_call", "message": {
            "content": "", "function_call": {"name": "register_balance", "arguments": {"register": "Р"}}}}]})

    app = web.Application()
    app.router.add_post("/v1/messages", anthropic)
    app.router.add_post("/oauth", oauth)
    app.router.add_post("/api/v1/chat/completions", gigachat)
    return app, seen


def test_adapters_against_fake_provider():
    async def run():
        app, seen = _fake_server()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as http:
                a = providers.Client({"provider": "anthropic", "base_url": base, "model": "m", "api_key": "sk"})
                ev = [e async for e in a.stream_chat(http, HISTORY, TOOLS)]
                g = providers.Client({"provider": "gigachat", "base_url": base + "/api/v1", "auth_url": base + "/oauth",
                                      "model": "GigaChat-2-Max", "api_key": "QmFzaWM="})
                gv = [e async for e in g.stream_chat(http, HISTORY, TOOLS)]
        finally:
            await runner.cleanup()
        return ev, gv, seen

    ev, gv, seen = asyncio.run(run())
    assert [e["text"] for e in ev if e["type"] == "text"] == ["Смотрю"]
    assert ev[-1]["tool_calls"][0]["function"] == {"name": "register_balance", "arguments": "{\"register\": \"Р\"}"}
    assert seen["anthropic"][0] == "sk" and seen["anthropic"][1]["system"] == "сис"
    assert gv[-1]["tool_calls"][0]["function"]["name"] == "register_balance"
    assert seen["oauth"] == ("Basic QmFzaWM=", {"scope": "GIGACHAT_API_PERS"})
    assert seen["gigachat"][0] == "Bearer tok"


def test_gigachat_schemas_get_properties():
    schema = {"type": "object", "properties": {"params": {"type": "object"}, "who": {"description": "имя или ссылка"},
              "items": {"type": "array", "items": {"type": "object"}}}}
    fixed = providers._strict_schema(schema)
    assert fixed["properties"]["params"]["properties"] == {}
    assert fixed["properties"]["params"]["additionalProperties"] is True
    assert fixed["properties"]["items"]["items"]["properties"] == {}
    assert fixed["properties"]["who"]["type"] == "string"
    assert schema["properties"]["params"] == {"type": "object"}  # исходная схема не изменена


def test_session_picks_up_profile_saved_elsewhere(session):
    """Профиль сменили из другого чата или приложения — открытый чат берёт его перед ходом и обновляет шапку."""
    assert session.profile["provider"] == "local"
    settings.save(session.base_id, settings.merge(session.base_id, {"provider": "openai", "api_key": "k", "model": "m",
                                                                    "cloud_consent": True}, "Админ"))
    asyncio.run(session.refresh_profile())
    assert session.profile["provider"] == "openai" and session.client.kind == "openai"
    assert session.events[-1]["type"] == "model" and session.events[-1]["label"].startswith("облако")
    asyncio.run(session.refresh_profile())  # без изменений — шапку не трогаем
    assert len(session.events) == 1
