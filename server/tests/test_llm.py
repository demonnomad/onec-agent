"""Потоковый клиент модели: разбор потока и обрыв молчащего потока по таймауту с понятной ошибкой."""
import asyncio

import aiohttp
from aiohttp import web
import pytest

from onec_agent import config, llm


async def _with_model(handler) -> list[dict]:
    """Поднимает «модель» на свободном порту и собирает события stream_chat к ней."""
    application = web.Application()
    application.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            return [ev async for ev in llm.stream_chat(session, [{"role": "user", "content": "привет"}], [],
                                                       base_url=f"http://127.0.0.1:{port}/v1", model="m", api_key="k")]
    finally:
        await runner.cleanup()


def _sse(*lines: str) -> bytes:
    return "".join(f"data: {line}\n\n" for line in lines).encode()


def test_stream_chat_collects_text_and_tool_calls():
    async def ok(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(_sse(
            '{"choices":[{"delta":{"content":"Го"}}]}',
            '{"choices":[{"delta":{"content":"тово"}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"query_run","arguments":"{\\"a\\":"}}]}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}',
            "[DONE]",
        ))
        return resp

    events = asyncio.run(_with_model(ok))
    assert [ev["text"] for ev in events[:-1]] == ["Го", "тово"]
    final = events[-1]
    assert final["content"] == "Готово"
    assert final["tool_calls"] == [{"id": "c1", "type": "function",
                                    "function": {"name": "query_run", "arguments": '{"a":1}'}}]
    assert final["finish_reason"] == "tool_calls"


def test_stream_chat_timeout_when_model_goes_silent(monkeypatch):
    monkeypatch.setattr(config, "LLM_READ_TIMEOUT_SEC", 1)

    async def silent(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(_sse('{"choices":[{"delta":{"content":"нач"}}]}'))
        await asyncio.sleep(3)  # модель «зависла»: поток открыт, данных нет
        return resp

    with pytest.raises(RuntimeError, match="не ответила за 1 с"):
        asyncio.run(_with_model(silent))
