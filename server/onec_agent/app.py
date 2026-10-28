"""HTTP + WebSocket сервер фундамента.

GET  /health   — проверка живости
GET  /ui.js, /ui.css — веб-интерфейс чата (подключается из поля HTML в 1С)
GET  /dev      — тот же чат в обычном браузере, без моста 1С (для разработки)
POST /upload   — вложение в диалог (multipart: поле session, затем file); заголовок X-Agent-Token
WS   /ws       — протокол событий; первым сообщением — hello с токеном (auth.py)
GET  /panel    — панель приложения (трей): встроенная модель, выгрузка базы; API /panel/api/* с X-Agent-Token
"""
import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from . import auth, bases, config, evals, files, local_model, providers, settings
from .agent import AgentSession

log = logging.getLogger("onec_agent")
WEB = Path(config.WEB_DIR)
HTTP = web.AppKey("http", aiohttp.ClientSession)  # общий HTTP-клиент: модель, распознавание скриншотов
IDLE = web.AppKey("idle", asyncio.Task)  # выгрузка встроенной модели при простое
SOCKETS = web.AppKey("sockets", set)  # открытые чаты — закрыть при остановке сервера


async def handle_settings(session: AgentSession, kind: str, data: dict, emit, http) -> None:
    """Окно настроек модели. Читать может любой (без ключа), менять и проверять — только администратор базы."""
    can_edit = bool(session.context.get("is_admin"))
    if kind == "settings_get":
        await emit({"type": "settings", "profile": settings.public(session.profile), "can_edit": can_edit,
                    "providers": {k: {f: v[f] for f in ("title", "cloud", "needs_key", "base_url", "model")}
                                  | ({"scope": v["scope"]} if "scope" in v else {})
                                  for k, v in providers.PROVIDERS.items()}})
        return
    if not can_edit:
        await emit({"type": "settings_error", "message": "Менять настройки модели может только администратор базы"})
        return
    try:
        profile = settings.merge(session.base_id, data.get("profile") or {}, session.context.get("user", ""),
                                 for_test=kind == "settings_test")
    except ValueError as exc:
        await emit({"type": "settings_error", "message": str(exc)})
        return
    if kind == "settings_test":
        try:
            result = await providers.test(http, profile)
        except Exception as exc:  # отказ провайдера показываем администратору как есть
            result = {"ok": False, "error": str(exc)[:400]}
        await emit({"type": "settings_test", **result})
        return
    settings.save(session.base_id, profile)
    session.apply_profile(profile)
    log.info("модель базы %s: %s", session.context.get("config_name"), providers.label(profile))
    await emit({"type": "model", **settings.public(profile), "can_edit": can_edit})
    await emit({"type": "settings_saved", "profile": settings.public(profile)})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)

    async def emit(event: dict) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps(event, ensure_ascii=False))

    session = AgentSession(emit, request.app[HTTP])
    session.reset()
    await emit({"type": "ready", "product": config.PRODUCT_NAME, "model": config.LLM_MODEL})

    authorized = False
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        kind = data.get("type")
        if not authorized:
            # До hello с верным токеном — ничего: ни модели, ни настроек, ни вызовов в 1С.
            if kind != "hello" or not auth.check(data.get("token")):
                log.warning("отказ в доступе: %s", "нет токена" if not data.get("token") else "неверный токен")
                await emit({"type": "auth_error", "message": "Нет доступа к серверу ИИ-консультанта: не указан "
                            "или неверный токен. Администратор: «Адрес сервера…» → токен из файла server/data/token."})
                await ws.close()
                break
            authorized = True
        if kind == "hello":
            session.session_id = data.get("session") or ""
            context = dict(data.get("context") or {})
            context.pop("token", None)  # в контекст модели токен не попадает
            session.set_context(context)
            log.info("hello: %s", {k: v for k, v in session.context.items() if k != "base_id"})
            await emit({"type": "model", **settings.public(session.profile),
                        "can_edit": bool(session.context.get("is_admin"))})
        elif kind in ("settings_get", "settings_save", "settings_test"):
            await handle_settings(session, kind, data, emit, request.app[HTTP])
        elif kind == "user_message":
            if session.busy:
                await emit({"type": "error", "message": "Агент ещё работает — дождитесь ответа или нажмите «Остановить»."})
            else:
                session.start_turn(data.get("text", ""), data.get("attachments") or [])
        elif kind == "run_evals":
            if session.busy:
                await emit({"type": "error", "message": "Агент ещё работает — дождитесь ответа или нажмите «Остановить»."})
            else:
                session.eval_task = asyncio.create_task(evals.run(session, emit, data.get("only") or None))
        elif kind == "tool_result":
            session.resolve_tool(data.get("id", ""), data)
        elif kind == "interrupt":
            session.interrupt()
        elif kind == "reset":
            session.interrupt()
            session.reset()
            if session.session_id:
                files.drop_session(session.session_id)
            await emit({"type": "reset_done"})

    session.interrupt()
    request.app[SOCKETS].discard(ws)
    return ws


def _static(name: str, ctype: str):
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(
            text=(WEB / name).read_text("utf-8"),
            content_type=ctype,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )
    return handler


CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "*"}


async def upload(request: web.Request) -> web.Response:
    """Приём вложения. Страница чата живёт в документе 1С (другой источник), отсюда CORS."""
    # Идентификатор диалога — полем формы, а не в URL: URL попадает в журнал доступа.
    if not auth.check(request.headers.get("X-Agent-Token")):
        return web.json_response({"ok": False, "error": "нет доступа: неверный токен"}, status=401, headers=CORS)
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "session":
            raise files.FileError("нет поля session")
        session = (await field.read()).decode()
        field = await reader.next()
        if field is None or field.name != "file":
            raise files.FileError("нет поля file")
        name = field.filename or "file"
        data = bytearray()
        while chunk := await field.read_chunk():
            data.extend(chunk)
            if len(data) > config.UPLOAD_MAX_BYTES:
                raise files.FileError(f"файл больше {config.UPLOAD_MAX_BYTES // (1024 * 1024)} МБ")
        meta = await files.save_upload(request.app[HTTP], session, name, bytes(data))
        log.info("upload: %s (%s, %d байт, частей %d)", meta["id"], meta["kind"], meta["size"], meta["parts"])
        return web.json_response({"ok": True, "file": meta}, headers=CORS)
    except files.FileError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=CORS)
    except Exception as exc:  # битый xlsx/pdf и т.п. — сообщаем пользователю, сервер живёт дальше
        log.exception("upload failed")
        return web.json_response({"ok": False, "error": f"не удалось прочитать файл: {exc}"}, status=400, headers=CORS)


async def upload_options(_request: web.Request) -> web.Response:
    return web.Response(headers=CORS)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "product": config.PRODUCT_NAME, "model": config.LLM_MODEL})


def _panel_auth(handler):
    async def wrapped(request: web.Request) -> web.Response:
        if not auth.check(request.headers.get("X-Agent-Token")):
            return web.json_response({"ok": False, "error": "нет доступа: неверный токен"}, status=401)
        try:
            return web.json_response({"ok": True, **(await handler(request) or {})})
        except (local_model.LocalModelError, bases.BaseError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return wrapped


async def _body(request: web.Request) -> dict:
    return await request.json() if request.can_read_body else {}


async def panel_status(_request):
    return local_model.manager.status()


async def panel_model_action(request):
    action = request.match_info["action"]
    data = await _body(request)
    m = local_model.manager
    if action == "download":
        m.start_download(data.get("preset") or local_model.settings()["preset"])
    elif action == "cancel":
        m.cancel_download()
    elif action == "start":
        if data.get("preset"):
            local_model.save_settings({"preset": data["preset"]})
        asyncio.create_task(_start_quietly(data.get("preset")))  # загрузка в память — до минуты, панель опрашивает статус
    elif action == "stop":
        await m.stop()
    elif action == "settings":
        local_model.save_settings(data)
    else:
        raise local_model.LocalModelError(f"Неизвестное действие {action}")
    return m.status()


async def panel_bases(_request):
    return {"bases": bases.list_bases(), "jobs": bases.jobs()}


async def panel_bases_add(request):
    data = await _body(request)
    bases.add_base(data.get("path", ""), data.get("title", ""), data.get("user", ""))
    return await panel_bases(request)


async def panel_export(request):
    data = await _body(request)
    bases.start_export(data.get("base_id", ""))
    return await panel_bases(request)


async def _start_quietly(preset):
    try:
        await local_model.manager.start(preset)
    except local_model.LocalModelError as exc:
        log.warning("запуск модели: %s", exc)


async def _on_startup(app: web.Application) -> None:
    files.cleanup_old(config.UPLOAD_TTL_HOURS)
    app[HTTP] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600))
    app[IDLE] = asyncio.create_task(local_model.manager.idle_watch())


async def _on_shutdown(app: web.Application) -> None:
    for ws in list(app[SOCKETS]):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"server shutdown")


async def _on_cleanup(app: web.Application) -> None:
    app[IDLE].cancel()
    await local_model.manager.stop()  # приложение закрыли — модель не должна остаться висеть в памяти
    await app[HTTP].close()


def make_app() -> web.Application:
    app = web.Application(client_max_size=config.UPLOAD_MAX_BYTES + 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/upload", upload)
    app.router.add_route("OPTIONS", "/upload", upload_options)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ui.js", _static("ui.js", "application/javascript"))
    app.router.add_get("/ui.css", _static("ui.css", "text/css"))
    app.router.add_get("/dev", _static("dev.html", "text/html"))
    app.router.add_get("/panel", _static("panel.html", "text/html"))
    app.router.add_get("/panel/api/status", _panel_auth(panel_status))
    app.router.add_post("/panel/api/model/{action}", _panel_auth(panel_model_action))
    app.router.add_get("/panel/api/bases", _panel_auth(panel_bases))
    app.router.add_post("/panel/api/bases/add", _panel_auth(panel_bases_add))
    app.router.add_post("/panel/api/bases/export", _panel_auth(panel_export))
    app.on_startup.append(_on_startup)
    app[SOCKETS] = set()
    app.on_shutdown.append(_on_shutdown)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    auth.token()  # создать токен при первом запуске, а не при первом подключении
    # Остановка не должна ждать открытые чаты минуту: приложение в трее гасит сервер при выходе,
    # а вместе с ним должна уйти и модель.
    web.run_app(make_app(), host=config.HOST, port=config.PORT, shutdown_timeout=3)


if __name__ == "__main__":
    main()
