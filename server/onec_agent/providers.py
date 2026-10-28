"""Источники модели: свой сервер (OpenAI-совместимый), облачные API — OpenAI-совместимые, Anthropic, GigaChat.

Профиль выбирает администратор базы в окне настроек консультанта; хранится на сервере агента
(settings.py), отдельно для каждой базы. Ключи API не попадают ни в базу 1С, ни в браузер после сохранения.
Все клиенты отдают одинаковые события, что и llm.stream_chat: {"type": "text"} и {"type": "final"}.
"""
import json
import time
import uuid
from typing import AsyncIterator

import aiohttp

from . import config, llm

PROVIDERS = {
    "local": {
        "title": "Свой сервер модели (LM Studio, Ollama, vLLM — в вашей сети)",
        "cloud": False, "needs_key": False,
        "base_url": config.LLM_BASE_URL, "model": config.LLM_MODEL,
    },
    "openai": {
        "title": "OpenAI-совместимый API (OpenAI, DeepSeek, OpenRouter, YandexGPT…)",
        "cloud": True, "needs_key": True,
        "base_url": "https://api.openai.com/v1", "model": "",
    },
    "anthropic": {
        "title": "Anthropic (Claude)",
        "cloud": True, "needs_key": True,
        "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5-5",
    },
    "gigachat": {
        "title": "GigaChat (Сбер)",
        "cloud": True, "needs_key": True,
        "base_url": "https://api.giga.chat/v1", "model": "GigaChat-3-Ultra",
        "auth_url": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth", "scope": "GIGACHAT_API_PERS",
    },
}

try:  # доверенные корневые сертификаты ОС (в т.ч. Минцифры), а не только набор Python
    import ssl as _ssl
    import truststore
    SYSTEM_SSL = truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
except ImportError:  # без truststore — стандартная проверка Python
    SYSTEM_SSL = None

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 4096


class ProviderError(Exception):
    pass


def default_profile() -> dict:
    return {"provider": "local", "base_url": config.LLM_BASE_URL, "model": config.LLM_MODEL}


def is_cloud(profile: dict) -> bool:
    return PROVIDERS.get(profile.get("provider"), {}).get("cloud", False)


def label(profile: dict) -> str:
    """Для шапки чата: куда уходят данные и какая модель."""
    info = PROVIDERS.get(profile.get("provider"), PROVIDERS["local"])
    where = "облако · " + info["title"].split(" (")[0] if info["cloud"] else "свой сервер"
    return f"{where} · {profile.get('model') or info['model'] or '?'}"


class Client:
    """Клиент модели по профилю. Токен GigaChat кэшируется на время жизни клиента."""

    def __init__(self, profile: dict):
        self.profile = profile
        self.info = PROVIDERS.get(profile.get("provider"), PROVIDERS["local"])
        self.kind = profile.get("provider") if profile.get("provider") in PROVIDERS else "local"
        self._gc_token = None
        self._gc_expires = 0.0

    @property
    def model(self) -> str:
        return self.profile.get("model") or self.info["model"]

    @property
    def base_url(self) -> str:
        return (self.profile.get("base_url") or self.info["base_url"]).rstrip("/")

    def stream_chat(self, http: aiohttp.ClientSession, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
        if self.kind == "anthropic":
            return self._anthropic(http, messages, tools)
        if self.kind == "gigachat":
            return self._gigachat(http, messages, tools)
        return llm.stream_chat(http, messages, tools, base_url=self.base_url, model=self.model,
                               api_key=self.profile.get("api_key") or config.LLM_API_KEY)

    # ---------- Anthropic Messages API ----------

    async def _anthropic(self, http, messages, tools):
        system, converted = _to_anthropic(messages)
        payload = {"model": self.model, "max_tokens": ANTHROPIC_MAX_TOKENS, "messages": converted,
                   "temperature": config.LLM_TEMPERATURE, "stream": True}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"]["description"],
                                 "input_schema": t["function"]["parameters"]} for t in tools]
        headers = {"x-api-key": self.profile.get("api_key", ""), "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        text_parts, blocks, stop = [], {}, None
        async with http.post(f"{self.base_url}/v1/messages", json=payload, headers=headers) as resp:
            if resp.status != 200:
                raise ProviderError(f"Anthropic HTTP {resp.status}: {(await resp.text())[:500]}")
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                kind = ev.get("type")
                if kind == "content_block_start":
                    block = ev.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        blocks[ev["index"]] = {"id": block["id"], "name": block["name"], "arguments": ""}
                elif kind == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta["text"])
                        yield {"type": "text", "text": delta["text"]}
                    elif delta.get("type") == "input_json_delta" and ev["index"] in blocks:
                        blocks[ev["index"]]["arguments"] += delta.get("partial_json", "")
                elif kind == "message_delta":
                    stop = (ev.get("delta") or {}).get("stop_reason") or stop
                elif kind == "error":
                    raise ProviderError(f"Anthropic: {ev.get('error')}")
        calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": b["arguments"] or "{}"}}
                 for _, b in sorted(blocks.items())]
        yield {"type": "final", "content": "".join(text_parts), "tool_calls": calls, "finish_reason": stop}

    # ---------- GigaChat ----------

    async def _gigachat_token(self, http) -> str:
        if self._gc_token and time.time() < self._gc_expires - 60:
            return self._gc_token
        headers = {"Authorization": f"Basic {self.profile.get('api_key', '')}", "RqUID": str(uuid.uuid4()),
                   "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        data = {"scope": self.profile.get("scope") or self.info["scope"]}
        async with http.post(self.profile.get("auth_url") or self.info["auth_url"], data=data, headers=headers,
                             ssl=self._ssl()) as resp:
            if resp.status != 200:
                raise ProviderError(f"GigaChat OAuth HTTP {resp.status}: {(await resp.text())[:300]}")
            body = await resp.json()
        self._gc_token = body["access_token"]
        self._gc_expires = body.get("expires_at", 0) / 1000 or time.time() + 1500
        return self._gc_token

    def _ssl(self):
        # Сертификаты Сбера выпущены НУЦ Минцифры. Доверие берём из системного хранилища (truststore):
        # установленный в ОС корневой сертификат Минцифры работает и здесь, как у curl и браузера.
        # Без него администратор может отключить проверку в настройках — осознанно, флажком.
        if not self.profile.get("verify_ssl", True):
            return False
        return SYSTEM_SSL

    async def _gigachat(self, http, messages, tools):
        token = await self._gigachat_token(http)
        payload = {"model": self.model, "messages": _to_gigachat(messages), "stream": False}
        if tools:
            payload["functions"] = [{"name": t["function"]["name"], "description": t["function"]["description"],
                                     "parameters": _strict_schema(t["function"]["parameters"])} for t in tools]
            payload["function_call"] = "auto"
        if config.LLM_TEMPERATURE > 0:
            payload["temperature"] = config.LLM_TEMPERATURE
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with http.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, ssl=self._ssl()) as resp:
            if resp.status != 200:
                raise ProviderError(f"GigaChat HTTP {resp.status}: {(await resp.text())[:500]}")
            body = await resp.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if content:
            yield {"type": "text", "text": content}
        calls = []
        fc = message.get("function_call")
        if fc:
            args = fc.get("arguments")
            calls.append({"id": f"gc_{uuid.uuid4().hex[:12]}", "type": "function",
                          "function": {"name": fc["name"],
                                       "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)}})
        yield {"type": "final", "content": content, "tool_calls": calls, "finish_reason": choice.get("finish_reason")}


def _strict_schema(schema):
    """GigaChat строже к схемам (иначе HTTP 422): у каждого объекта должно быть поле properties, у каждого
    поля — тип. Свободным объектам вроде params у query_run добавляем пустые properties с разрешением
    любых полей, полям без типа
    («наименование или ссылка») — тип string: наименование строкой 1С и так понимает."""
    if isinstance(schema, dict):
        fixed = {k: (_strict_schema(v) if k != "properties" else
                     {name: _strict_schema(_typed(prop)) for name, prop in v.items()})
                 for k, v in schema.items()}
        if fixed.get("type") == "object" and "properties" not in fixed:
            # Без additionalProperties пустой список полей GigaChat понимает как «объект пустой»
            # и шлёт params: {} — запрос падает без значений параметров.
            fixed["properties"] = {}
            fixed.setdefault("additionalProperties", True)
        return fixed
    if isinstance(schema, list):
        return [_strict_schema(v) for v in schema]
    return schema


def _typed(prop):
    return {**prop, "type": "string"} if isinstance(prop, dict) and "type" not in prop else prop


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-формат истории → Anthropic: system отдельно, вызовы — блоки tool_use, результаты —
    tool_result в сообщении пользователя; подряд идущие сообщения одной роли склеиваются."""
    system, out = [], []

    def push(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            system.append(content or "")
        elif role == "user":
            push("user", [{"type": "text", "text": content or ""}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": content}] if content else []
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": content or ""}])
    return "\n\n".join(system), out


def _to_gigachat(messages: list[dict]) -> list[dict]:
    """OpenAI-формат → GigaChat: вызов — function_call (по одному на сообщение), результат — роль function
    с именем функции и JSON-содержимым."""
    names, out = {}, []
    for m in messages:
        role = m.get("role")
        if role in ("system", "user"):
            out.append({"role": role, "content": m.get("content") or ""})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            msg = {"role": "assistant", "content": m.get("content") or ""}
            if calls:
                c = calls[0]
                names[c["id"]] = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                msg["function_call"] = {"name": c["function"]["name"], "arguments": args}
            out.append(msg)
        elif role == "tool":
            content = m.get("content") or ""
            try:
                json.loads(content)
            except json.JSONDecodeError:
                content = json.dumps({"result": content}, ensure_ascii=False)
            out.append({"role": "function", "name": names.get(m.get("tool_call_id"), "tool"), "content": content})
    return out


async def test(http: aiohttp.ClientSession, profile: dict) -> dict:
    """Проверка подключения: короткий запрос без инструментов, ответ и время."""
    started = time.time()
    client = Client(profile)
    text = ""
    async for ev in client.stream_chat(http, [{"role": "user", "content": "Ответь одним словом: готово"}], []):
        if ev["type"] == "final":
            text = ev["content"]
    return {"ok": True, "reply": text.strip()[:200], "seconds": round(time.time() - started, 1)}
