"""Тестовый набор: вопросы к базе с эталонами, которые считаются живым запросом.

Для каждого случая: эталонный запрос выполняется в 1С (через тот же мост, что и инструменты),
затем агент отвечает на вопрос с чистого листа, и все числа эталона должны найтись в ответе.
Эталон считается в момент прогона — тесты не устаревают при изменении данных базы.

Наборы: evals/<Метаданные.Имя>.json. Запуск — командой /тесты в чате.
"""
import asyncio
import datetime as dt
import json
import re
import time
from pathlib import Path

from . import config, files, providers

EVALS_DIR = Path(config.EVALS_DIR)
NUMBER = re.compile(r"-?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?")


def load_suite(config_name: str) -> dict:
    """{"guard": {...}, "cases": [...]} — или старый формат: просто список вопросов."""
    path = EVALS_DIR / f"{config_name}.json"
    data = json.loads(path.read_text("utf-8")) if path.is_file() else []
    return data if isinstance(data, dict) else {"cases": data}


def suite_variants(config_name: str) -> list[dict]:
    """Особые наборы для конкретных баз (evals/<конфигурация>.<вариант>.json, например демо-база):
    у каждого есть запрос detect — набор берётся, если запрос в базе вернул хотя бы одну строку."""
    out = []
    for path in sorted(EVALS_DIR.glob(f"{config_name}.*.json")):
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and data.get("detect"):
            out.append({**data, "file": path.name})
    return out


def load_cases(config_name: str) -> list[dict]:
    return load_suite(config_name).get("cases", [])


def _params(params: dict) -> dict:
    today = dt.date.today()
    special = {"$today": today.isoformat(), "$tomorrow": (today + dt.timedelta(days=1)).isoformat(),
               "$days_ago_10": (today - dt.timedelta(days=10)).isoformat()}
    return {k: special.get(v, v) for k, v in (params or {}).items()}


def expected_numbers(result_json: str) -> list[float]:
    data = json.loads(result_json)
    rows = data.get("rows") or []
    if not rows:
        return [0.0]
    return [float(v) for v in rows[0] if isinstance(v, (int, float)) and not isinstance(v, bool)]


def answer_numbers(text: str) -> list[float]:
    found = []
    for raw in NUMBER.findall(text):
        cleaned = re.sub(r"[   ]", "", raw).replace(",", ".")
        try:
            found.append(float(cleaned))
        except ValueError:
            continue
    return found


# Нулевой эталон модель часто называет словами: «дебетовое сальдо отсутствует», «ничего не должны».
ZERO_WORDS = re.compile(r"ноль|нулев|равно нулю|отсутству|ничего не долж|не должны|нет долга|долга нет|не зарегистрир"
                        r"|не выдан|не выставл|не было|не найден|не обнаружен|нет ни одн", re.IGNORECASE)


def check(expected: list[float], answer: str) -> tuple[bool, list[float]]:
    got = answer_numbers(answer)
    if ZERO_WORDS.search(answer):
        got.append(0.0)
    # По модулю: «кредитовое сальдо 5 840,67» — верный ответ на эталон −5 840,67.
    missing = [e for e in expected if not any(abs(abs(g) - abs(e)) <= 0.006 for g in got)]
    return not missing, missing


def fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")


# Инструменты, которые в тестовом прогоне не выполняются: меняют данные или открывают формы у пользователя.
DRY_RUN_TOOLS = {"invoice_create", "sale_create", "purchase_create", "create_based_on", "object_create",
                 "object_update", "document_post", "document_unpost", "open_list", "ui_open", "month_close_run"}


def _numbers_in(value) -> set[float]:
    found = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _numbers_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= _numbers_in(v)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        found.add(float(value))
    return found


# Уточнение без знака вопроса: «Уточните, пожалуйста, по какому счёту…», «Укажите период».
ASK_WORDS = re.compile(r"^\s*(уточните|укажите|назовите|подскажите)\b", re.IGNORECASE | re.MULTILINE)


def check_behavior(checks: dict, tools: list[str], answer: str, actions: list | None = None) -> list[str]:
    """Нарушения правил поведенческого теста (пусто — тест пройден). actions — [(инструмент, аргументы)]."""
    actions = actions or []
    fails = []
    expect = checks.get("expect_any") or []
    if expect and not set(expect) & set(tools):
        fails.append("не вызван ни один из: " + ", ".join(expect))
    for name in checks.get("forbid") or []:
        if name in tools:
            fails.append(f"вызван {name}, а не должен")
    if checks.get("forbid_actions"):
        used = sorted(set(tools) & DRY_RUN_TOOLS)
        if used:
            fails.append("попытка действия без запроса: " + ", ".join(used))
    if checks.get("max_steps") is not None and len(tools) > checks["max_steps"]:
        fails.append(f"шагов {len(tools)} при лимите {checks['max_steps']}")
    if checks.get("must_ask") and "?" not in answer and "ask_user" not in tools and not ASK_WORDS.search(answer):
        fails.append("нет уточняющего вопроса")
    for spec in checks.get("action_numbers") or []:
        # Действие вызвано именно с названными пользователем числами (например, 2 шт по 3000).
        calls = [a for name, a in actions if name == spec["tool"]]
        if not any(set(spec["numbers"]) <= _numbers_in(a) for a in calls):
            fails.append(f"{spec['tool']} не вызван с числами {spec['numbers']}")
    for spec in checks.get("expect_args") or []:
        # Инструмент вызван с нужным аргументом (например, object_find с отбором filters).
        if not any(name == spec["tool"] and a.get(spec["has"]) for name, a in actions):
            fails.append(f"{spec['tool']} не вызван с {spec['has']}")
    wanted = checks.get("answer_any") or []
    if wanted and not any(re.search(w, answer, re.IGNORECASE) for w in wanted):
        fails.append("в ответе нет ничего из: " + " | ".join(wanted))
    for want in checks.get("answer_all") or []:
        if not re.search(want, answer, re.IGNORECASE):
            fails.append(f"в ответе нет: «{want}»")
    for bad in checks.get("answer_none") or []:
        if re.search(bad, answer, re.IGNORECASE):
            fails.append(f"в ответе недопустимое: «{bad}»")
    return fails


async def run(session, emit, only: str | None = None) -> None:
    """Прогоняет набор для конфигурации сессии. emit — исходный канал в интерфейс.
    only: «числа» — только сверка с эталоном, «поведение» — только поведенческие тесты, иначе — начало id тестов."""
    config_name = session.context.get("config_name", "")
    suite = load_suite(config_name)
    for variant in suite_variants(config_name):
        event = {"type": "tool_call", "id": f"eval-detect-{time.time()}", "name": "query_run",
                 "args": {"text": variant["detect"]["text"], "params": _params(variant["detect"].get("params")), "limit": 1},
                 "title": "Тесты: определяю набор для этой базы", "kind": "read", "needs_approval": False, "executor": "client"}
        res = await session._run_client_tool(event["id"], event)
        if res.get("ok") and json.loads(res["result"]).get("rows"):
            suite = variant
            await emit({"type": "text_delta", "text": f"Набор тестов: {variant['file']}\n"})
            break
    cases = suite.get("cases", [])
    if only and any(c["id"].startswith(only) for c in cases):
        cases = [c for c in cases if c["id"].startswith(only)]  # /тесты b_ext — тесты с таким началом id
    elif only:
        behavior = only.startswith("повед") or only.startswith("behav")
        cases = [c for c in cases if ("checks" in c or "turns" in c) == behavior]
    if not cases:
        await emit({"type": "error", "message": f"Нет тестового набора evals/{session.context.get('config_name')}.json"})
        await emit({"type": "turn_done"})
        return

    answer_parts: list[str] = []

    turn_errors: list[str] = []

    async def quiet_emit(event: dict) -> None:
        # Внутренние ходы агента показываем, но их turn_done не завершает весь прогон в интерфейсе.
        if event.get("type") == "turn_done":
            return
        if event.get("type") == "text_delta":
            answer_parts.append(event["text"])
        if event.get("type") == "error":
            turn_errors.append(str(event.get("message", ""))[:120])
        await emit(event)

    session.stop_requested = False
    session.dry_run_tools = DRY_RUN_TOOLS
    temp_session = ""  # вложения тестов складываются во временную сессию файлов
    original_emit = session.emit
    session.emit = quiet_emit
    results = []

    async def snapshot() -> set | None:
        """Снимок документов для сторожа: прогон с действиями не должен ничего изменить в базе."""
        query = (suite.get("guard") or {}).get("query")
        if not query or not any("checks" in c or "turns" in c for c in cases):
            return None
        event = {"type": "tool_call", "id": f"eval-guard-{time.time()}", "name": "query_run",
                 "args": {"text": query["text"], "params": _params(query.get("params")), "limit": 500},
                 "title": "Сторож: снимок документов базы", "kind": "read", "needs_approval": False, "executor": "client"}
        res = await session._run_client_tool(event["id"], event)
        if not res.get("ok"):
            await emit({"type": "error", "message": f"Сторож не смог снять снимок: {res.get('error')}"})
            return None
        return {tuple(r) for r in json.loads(res["result"]).get("rows", [])}

    try:
        before = await snapshot()
        for i, case in enumerate(cases, 1):
            first = case.get("question") or case["turns"][0]["question"]
            await emit({"type": "text_delta", "text": f"\n### Тест {i}/{len(cases)} · {case['id']}\n{first}\n"})
            await emit({"type": "text_end"})
            expected = [float(x) for x in case["expected"]] if case.get("expected") else None
            if "reference" in case:
                ref_event = {
                    "type": "tool_call", "id": f"eval-{case['id']}-{int(time.time())}", "name": "query_run",
                    "args": {"text": case["reference"]["text"], "params": _params(case["reference"].get("params")), "limit": 5},
                    "title": "Эталон: считаю правильный ответ запросом", "kind": "read", "needs_approval": False,
                    "executor": "client",
                }
                ref = await session._run_client_tool(ref_event["id"], ref_event)
                if not ref.get("ok"):
                    results.append({"id": case["id"], "ok": False, "reason": "эталон не выполнился: " + str(ref.get("error"))})
                    await emit({"type": "error", "message": f"Эталон не выполнился: {ref.get('error')}"})
                    continue
                expected = expected_numbers(ref["result"])

            session.reset()
            session.asked = []
            started = time.time()
            fails, tools, guarded, notes, answer = [], [], [], [], ""
            # Многоходовой сценарий — реплики одного диалога; правила проверяются на каждом ходе.
            turns = case.get("turns") or [{"question": case["question"], "checks": case.get("checks"), "file": case.get("file")}]
            for n, step in enumerate(turns, 1):
                if n > 1:
                    await emit({"type": "text_delta", "text": f"\n› {step['question']}\n"})
                    await emit({"type": "text_end"})
                answer_parts.clear()
                turn_errors.clear()
                mark, asked_before = len(session.messages), len(session.asked)
                attachments = []
                if step.get("file"):  # вложение к вопросу — как если бы пользователь приложил файл
                    if not session.session_id:
                        session.session_id = f"eval-{int(time.time())}"
                        temp_session = session.session_id
                    meta = await files.save_upload(session.http, session.session_id, step["file"]["name"],
                                                   step["file"]["text"].encode("utf-8"))
                    attachments.append(meta["id"])
                session.start_turn(step["question"], attachments)
                await session.turn
                if session.stop_requested:
                    raise asyncio.CancelledError
                answer = "".join(answer_parts)
                calls = [c for m in session.messages[mark:] if m.get("role") == "assistant"
                         for c in (m.get("tool_calls") or [])]
                # Действие, которое перехватила защита, до 1С не дошло — пользователь диалога не видел.
                # Это не нарушение, но слабое место модели: показываем отдельно.
                guarded += [c["function"]["name"] for c in calls if c["id"] in session.guarded_ids]
                calls = [c for c in calls if c["id"] not in session.guarded_ids]
                turn_tools = [c["function"]["name"] for c in calls]
                tools += turn_tools
                actions = []
                for c in calls:
                    try:
                        actions.append((c["function"]["name"], json.loads(c["function"]["arguments"] or "{}")))
                    except json.JSONDecodeError:
                        pass
                prefix = f"ход {n}: " if len(turns) > 1 else ""
                fails += [prefix + "ход прерван: " + e for e in turn_errors]
                if step.get("checks"):
                    # Варианты-кнопки — часть ответа: «Павел или Мария?» может быть кнопками при коротком вопросе.
                    buttons = " ".join(" ".join(q["options"]) for q in session.asked[asked_before:])
                    fails += [prefix + f for f in check_behavior(step["checks"], turn_tools, answer + "\n" + buttons, actions)]
                    if step["checks"].get("must_ask") and "ask_user" not in turn_tools:
                        notes.append(prefix + "вопрос текстом, без кнопок ask_user")
            if expected is not None:
                passed, missing = check(expected, answer)
                if not passed:
                    fails.append("нет в ответе: " + ", ".join(fmt(m) for m in missing))
            results.append({"id": case["id"], "ok": not fails, "fails": fails, "expected": expected, "tools": tools,
                            "guarded": guarded, "notes": notes, "asked": list(session.asked),
                            "seconds": round(time.time() - started), "steps": len(tools), "answer": answer})
            verdict = "✅ верно" if not fails else "❌ " + "; ".join(fails)
            if guarded:
                verdict += " · 🛡 перехвачено защитой: " + ", ".join(guarded)
            if notes:
                verdict += " · ✎ " + "; ".join(notes)
            tail = f" · эталон: {', '.join(fmt(e) for e in expected)}" if expected is not None else f" · шаги: {', '.join(tools) or '—'}"
            await emit({"type": "text_end"})
            await emit({"type": "text_delta", "text": f"\n**{verdict}**{tail}\n"})
            await emit({"type": "text_end"})
        if before is not None:
            after = await snapshot()
            if after is not None:
                changed = sorted({r[0] for r in (after - before) | (before - after)})
                results.append({"id": "_guard", "ok": not changed,
                                "fails": [f"база изменилась: {', '.join(changed[:10])}"] if changed else []})
                await emit({"type": "text_end"})
                await emit({"type": "text_delta", "text": "\n**" + ("✅ сторож: база не изменилась" if not changed else
                            "❌ сторож: тестовый прогон изменил базу — " + ", ".join(changed[:10])) + "**\n"})
                await emit({"type": "text_end"})
    except asyncio.CancelledError:
        await emit({"type": "turn_done", "interrupted": True})
        return
    finally:
        session.emit = original_emit
        session.dry_run_tools = set()
        if temp_session:
            files.drop_session(temp_session)
            session.session_id = ""

    passed = sum(r["ok"] for r in results)
    lines = [f"\n## Итог: {passed} из {len(results)}\n", "| Тест | Результат | Время | Шагов | 🛡 |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {'✅' if r['ok'] else '❌'} | {r.get('seconds', '—')} с | {r.get('steps', '—')} | "
                     f"{len(r.get('guarded') or []) or ''} |")
    # Перехваты защитой — не провал, но мера того, насколько модель действует раньше, чем спрашивает.
    shielded = [r for r in results if r.get("guarded")]
    lines.append(f"\nДействий перехвачено защитой: {sum(len(r['guarded']) for r in shielded)}"
                 + (" (" + ", ".join(r["id"] for r in shielded) + ")" if shielded else ""))
    texted = [r for r in results if r.get("notes")]
    if texted:
        lines.append("Уточнения текстом, без кнопок: " + ", ".join(r["id"] for r in texted))
    await emit({"type": "text_delta", "text": "\n".join(lines) + "\n"})

    log_dir = Path(config.AUDIT_LOG).parent / "evals"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (log_dir / f"{stamp}.json").write_text(json.dumps({"model": providers.label(session.profile), "results": results},
                                                      ensure_ascii=False, indent=1), "utf-8")
    session.reset()
    await emit({"type": "turn_done"})
