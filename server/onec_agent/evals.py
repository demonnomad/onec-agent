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

from . import config, providers

EVALS_DIR = Path(__file__).parent.parent / "evals"
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
ZERO_WORDS = re.compile(r"ноль|нулев|равно нулю|отсутству|ничего не долж|не должны|нет долга|долга нет", re.IGNORECASE)


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
                 "object_update", "document_post", "document_unpost", "open_list", "ui_open"}


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
    if checks.get("must_ask") and "?" not in answer and "ask_user" not in tools:
        fails.append("нет уточняющего вопроса")
    for spec in checks.get("action_numbers") or []:
        # Действие вызвано именно с названными пользователем числами (например, 2 шт по 3000).
        calls = [a for name, a in actions if name == spec["tool"]]
        if not any(set(spec["numbers"]) <= _numbers_in(a) for a in calls):
            fails.append(f"{spec['tool']} не вызван с числами {spec['numbers']}")
    wanted = checks.get("answer_any") or []
    if wanted and not any(re.search(w, answer, re.IGNORECASE) for w in wanted):
        fails.append("в ответе нет ничего из: " + " | ".join(wanted))
    for bad in checks.get("answer_none") or []:
        if re.search(bad, answer, re.IGNORECASE):
            fails.append(f"в ответе недопустимое: «{bad}»")
    return fails


async def run(session, emit, only: str | None = None) -> None:
    """Прогоняет набор для конфигурации сессии. emit — исходный канал в интерфейс.
    only: «числа» — только сверка с эталоном, «поведение» — только поведенческие тесты."""
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
    if only:
        behavior = only.startswith("повед") or only.startswith("behav")
        cases = [c for c in cases if ("checks" in c or "turns" in c) == behavior]
    if not cases:
        await emit({"type": "error", "message": f"Нет тестового набора evals/{session.context.get('config_name')}.json"})
        await emit({"type": "turn_done"})
        return

    answer_parts: list[str] = []

    async def quiet_emit(event: dict) -> None:
        # Внутренние ходы агента показываем, но их turn_done не завершает весь прогон в интерфейсе.
        if event.get("type") == "turn_done":
            return
        if event.get("type") == "text_delta":
            answer_parts.append(event["text"])
        await emit(event)

    session.stop_requested = False
    session.dry_run_tools = DRY_RUN_TOOLS
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
            started = time.time()
            fails, tools, answer = [], [], ""
            # Многоходовой сценарий — реплики одного диалога; правила проверяются на каждом ходе.
            turns = case.get("turns") or [{"question": case["question"], "checks": case.get("checks")}]
            for n, step in enumerate(turns, 1):
                if n > 1:
                    await emit({"type": "text_delta", "text": f"\n› {step['question']}\n"})
                    await emit({"type": "text_end"})
                answer_parts.clear()
                mark = len(session.messages)
                session.start_turn(step["question"])
                await session.turn
                if session.stop_requested:
                    raise asyncio.CancelledError
                answer = "".join(answer_parts)
                calls = [c for m in session.messages[mark:] if m.get("role") == "assistant"
                         for c in (m.get("tool_calls") or [])]
                turn_tools = [c["function"]["name"] for c in calls]
                tools += turn_tools
                actions = []
                for c in calls:
                    try:
                        actions.append((c["function"]["name"], json.loads(c["function"]["arguments"] or "{}")))
                    except json.JSONDecodeError:
                        pass
                prefix = f"ход {n}: " if len(turns) > 1 else ""
                if step.get("checks"):
                    fails += [prefix + f for f in check_behavior(step["checks"], turn_tools, answer, actions)]
            if expected is not None:
                passed, missing = check(expected, answer)
                if not passed:
                    fails.append("нет в ответе: " + ", ".join(fmt(m) for m in missing))
            results.append({"id": case["id"], "ok": not fails, "fails": fails, "expected": expected, "tools": tools,
                            "seconds": round(time.time() - started), "steps": len(tools), "answer": answer})
            verdict = "✅ верно" if not fails else "❌ " + "; ".join(fails)
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

    passed = sum(r["ok"] for r in results)
    lines = [f"\n## Итог: {passed} из {len(results)}\n", "| Тест | Результат | Время | Шагов |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {'✅' if r['ok'] else '❌'} | {r.get('seconds', '—')} с | {r.get('steps', '—')} |")
    await emit({"type": "text_delta", "text": "\n".join(lines) + "\n"})

    log_dir = Path(config.AUDIT_LOG).parent / "evals"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (log_dir / f"{stamp}.json").write_text(json.dumps({"model": providers.label(session.profile), "results": results},
                                                      ensure_ascii=False, indent=1), "utf-8")
    session.reset()
    await emit({"type": "turn_done"})
