"""Потоковый клиент OpenAI-совместимого chat/completions с поддержкой tool calls."""
import json
from typing import AsyncIterator

import aiohttp

from . import config


async def stream_chat(session: aiohttp.ClientSession, messages: list[dict], tools: list[dict], *,
                      base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                      temperature: float | None = None) -> AsyncIterator[dict]:
    """Отдаёт события: {"type": "text", "text": ...} по мере генерации и в конце
    {"type": "final", "content": str, "tool_calls": [...], "finish_reason": str}.
    Без параметров — модель из config (локальный сервер по умолчанию)."""
    payload = {
        "model": model or config.LLM_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "stream": True,
    }
    if tools:  # часть провайдеров отвергает пустой список инструментов
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {api_key or config.LLM_API_KEY}"}
    base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
    content_parts: list[str] = []
    calls: dict[int, dict] = {}
    finish_reason = None

    async with session.post(f"{base_url}/chat/completions", json=payload, headers=headers) as resp:
        if resp.status != 200:
            raise RuntimeError(f"LLM HTTP {resp.status}: {(await resp.text())[:500]}")
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if not chunk.get("choices"):
                continue
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                yield {"type": "text", "text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    tool_calls = []
    for i in sorted(calls):
        c = calls[i]
        tool_calls.append({
            "id": c["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
        })
    yield {"type": "final", "content": "".join(content_parts), "tool_calls": tool_calls, "finish_reason": finish_reason}
