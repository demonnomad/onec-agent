"""Защиты от выдумок модели и подсказки в результатах инструментов."""
import asyncio
import json

from onec_agent.agent import _explain_totals


def test_invented_prices_are_blocked(session):
    session.messages.append({"role": "user", "content": "Выстави счёт покупателю Альфа-Инжиниринг на услугу прошивки ККТ"})
    args = {"counterparty": {"repr": "АЛЬФА-ИНЖИНИРИНГ ООО"},
            "items": [{"item": {"repr": "Услуга по обновлению прошивки ККТ"}, "quantity": 1, "price": 5000}]}
    err = session._unconfirmed_numbers("invoice_create", args)
    assert err.startswith("Не создаю:") and "цена 5000" in err and "количество 1" in err and "ask_user" in err


def test_everything_named_by_user_passes(session):
    session.messages.append({"role": "user", "content": "Счёт Ромашке: 10 кабелей ВВГ по 1 500,50 руб"})
    args = {"counterparty": "Ромашка ООО", "items": [{"item": "Кабель ВВГ 3x2.5", "quantity": 10, "price": 1500.5}]}
    assert session._unconfirmed_numbers("invoice_create", args) is None


def test_invented_item_and_counterparty_are_blocked(session):
    session.messages.append({"role": "user", "content": "Выстави счёт покупателю Альфа-Инжиниринг"})
    args = {"counterparty": {"repr": "АЛЬФА-ИНЖИНИРИНГ ООО"},
            "items": [{"item": {"repr": "Услуга по обновлению прошивки ККТ"}, "quantity": 2, "price": 3000}]}
    missing = " ".join(session._missing_data("invoice_create", args))
    assert "позиция «Услуга по обновлению прошивки ККТ»" in missing and "контрагент" not in missing


def test_generic_words_do_not_count_as_named(session):
    session.messages.append({"role": "user", "content": "Выстави счёт ИП на 2 услуги по 3000"})
    args = {"counterparty": {"repr": "Громов Илья Сергеевич ИП"},
            "items": [{"item": {"repr": "Услуга по обновлению прошивки ККТ"}, "quantity": 2, "price": 3000}]}
    missing = " ".join(session._missing_data("invoice_create", args))
    assert "контрагент («Громов" in missing and "прошивки ККТ" in missing


def test_empty_document_asks_for_contents(session):
    session.messages.append({"role": "user", "content": "Сделай реализацию для Сидоренко"})
    missing = session._missing_data("sale_create", {"counterparty": "Сидоренко", "items": []})
    assert missing == ["что именно в документе (товары или услуги)"]


def test_price_proposed_by_agent_needs_user_answer(session):
    session.messages.append({"role": "user", "content": "реализация 10 кабелей ВВГ Ромашке"})
    session.messages.append({"role": "assistant", "content": "Цена кабеля ВВГ в прайсе 700 руб. Подойдёт?"})
    args = {"counterparty": "Ромашка", "items": [{"item": "Кабель ВВГ", "quantity": 10, "price": 700}]}
    assert session._unconfirmed_numbers("sale_create", args)
    session.messages.append({"role": "user", "content": "да"})
    assert session._unconfirmed_numbers("sale_create", args) is None


def test_ask_user_ends_turn_with_options(session):
    calls = [{"id": "q1", "type": "function", "function": {"name": "ask_user", "arguments": json.dumps(
        {"question": "Какие услуги и по какой цене?", "options": ["Прошивка ККТ", "Обслуживание ККТ"]},
        ensure_ascii=False)}}]

    async def fake_stream(http, messages, tools):
        yield {"type": "final", "content": "", "tool_calls": calls, "finish_reason": "tool_calls"}

    session.client.stream_chat = fake_stream
    session.messages.append({"role": "user", "content": "Выстави счёт Альфа-Инжиниринг"})
    session.messages.append({"role": "tool", "tool_call_id": "t0",  # варианты найдены в базе
                             "content": '{"items": [{"repr": "Прошивка ККТ"}, {"repr": "Обслуживание ККТ"}]}'})
    asyncio.run(session._run_turn())
    kinds = [e["type"] for e in session.events]
    assert kinds[-1] == "turn_done" and {"type": "ask", "options": ["Прошивка ККТ", "Обслуживание ККТ"]} in session.events
    assert session.messages[-1]["role"] == "assistant" and session.messages[-1]["content"].startswith("Какие услуги")
    assert session.messages[-2] == {"role": "tool", "tool_call_id": "q1",
                                    "content": session.messages[-2]["content"]}  # история валидна для API


def test_other_tools_are_not_checked(session):
    assert session._unconfirmed_numbers("query_run", {"items": [{"price": 1}]}) is None
    session.messages.append({"role": "user", "content": "Заведи номенклатуру «Монтаж кабеля»"})
    assert session._unconfirmed_numbers("object_create", {"fields": {"Наименование": "Монтаж кабеля"}}) is None
    assert session._unconfirmed_numbers("object_create", {"fields": {"Наименование": "Прокладка трассы"}})


def test_date_hint_picks_document(session):
    session.messages.append({"role": "user", "content": "Проведи реализацию ДС00-000002 от 26.10.2026"})
    found = {"items": [{"repr": "Р ДС00-000002 от 26.10.2026", "date": "2026-10-26T12:16:44"},
                       {"repr": "Р ДС00-000002 от 13.01.2021", "date": "2021-01-13T17:51:35"}]}
    hint = json.loads(session._date_hint(json.dumps(found)))["hint"]
    assert "26.10.2026" in hint


def test_declines_and_guard_are_not_errors(session):
    for error in ("Пользователь отклонил действие", "Не создаю: цена 5000"):
        session._tool_result_text("invoice_create", {}, {"ok": False, "error": error})
    assert session.errors_in_row == 0
    session._tool_result_text("query_run", {}, {"ok": False, "error": "{(1,1)}: Поле не найдено \"Т.Х\""})
    assert session.errors_in_row == 1


def test_query_error_gets_hint(session):
    text = session._tool_result_text("query_run", {}, {"ok": False, "error": "Поле не входит в группу \"Т.Дата\""})
    assert "ПЕРВЫЕ 1" in json.loads(text)["hint"]


def test_balance_totals_are_spelled_out_and_rows_named():
    raw = json.dumps({"period": "на 30.09.2026", "columns": ["Субконто1", "СуммаОстатокДт", "СуммаОстатокКт"],
                      "rows": [[{"repr": "Громов", "_ref": "Справочник.Контрагенты", "uuid": "1"}, 227998.09, 0],
                               [{"repr": "Благоустройство", "_ref": "x", "uuid": "2"}, 0, 953881.17]],
                      "totals": {"СуммаОстатокДт": 227998.09, "СуммаОстатокКт": 953881.17}, "query": "ВЫБРАТЬ …"})
    text = _explain_totals("register_balance", raw)
    head, body = text.split("\n", 2)[:2], json.loads(text.split("\n")[-1])
    assert "сальдо по Кт = 953 881,17" in head[0] and "кредитовое сальдо 725 883,08" in head[0]
    assert "Наибольшее дебетовое: Громов" in head[1]
    assert body["rows"][0] == {"Субконто1": "Громов", "СуммаОстатокДт": 227998.09, "СуммаОстатокКт": 0}
    assert "query" not in body


def test_answer_after_db_error_is_marked(session):
    """Ответ модели после ошибки обращения к базе получает предупреждение."""
    from onec_agent import agent

    async def fake_stream(http, messages, tools):
        yield {"type": "final", "content": "Контрагентов 124.", "tool_calls": [], "finish_reason": "stop"}

    session.client.stream_chat = fake_stream
    session.errors_in_row = 1
    session.messages.append({"role": "user", "content": "сколько контрагентов"})
    asyncio.run(session._run_turn())
    texts = "".join(e.get("text", "") for e in session.events if e["type"] == "text_delta")
    assert "не подтверждены данными" in texts
    assert agent.MAX_ERRORS_IN_ROW == 3


def test_dry_run_never_reaches_1c(session):
    """Регрессия 27.10: в тестовом прогоне действие уходило на страницу как клиентское — и 1С его выполняла."""
    session.dry_run_tools = {"invoice_create"}
    session.messages.append({"role": "user", "content": "Выстави счёт Ромашке: 2 кабеля по 300"})
    call = {"id": "c1", "function": {"name": "invoice_create",
                                     "arguments": '{"counterparty": "Ромашка", "items": [{"item": "кабель", "quantity": 2, "price": 300}]}'}}
    result = asyncio.run(session._call_tool(call))
    shown = [e for e in session.events if e["type"] == "tool_call"]
    assert shown and all(e["executor"] == "server" and e.get("dry_run") for e in shown)
    assert not session.pending  # ответа от 1С никто не ждёт
    assert "тестовый прогон" in json.loads(result)["error"]


def test_invented_numbers_never_reach_1c(session):
    session.messages.append({"role": "user", "content": "Выстави счёт Ромашке"})
    call = {"id": "c2", "function": {"name": "invoice_create",
                                     "arguments": '{"counterparty": "Ромашка", "items": [{"item": "x", "quantity": 1, "price": 5000}]}'}}
    result = asyncio.run(session._call_tool(call))
    assert all(e["executor"] == "server" for e in session.events if e["type"] == "tool_call")
    assert json.loads(result)["error"].startswith("Не создаю:")


def test_quantity_in_words_counts_as_named(session):
    session.messages.append({"role": "user", "content": "Одна «Услуга по обновлению прошивки ККТ» Сидоренко за 1500 рублей"})
    args = {"counterparty": "Сидоренко", "items": [{"item": "Услуга по обновлению прошивки ККТ", "quantity": 1, "price": 1500}]}
    assert session._unconfirmed_numbers("sale_create", args) is None
    session.messages.append({"role": "user", "content": "и две таких же Ромашке"})
    assert 2.0 in __import__("onec_agent.agent", fromlist=["_numbers"])._numbers("две таких же")
    assert 1.0 not in __import__("onec_agent.agent", fromlist=["_numbers"])._numbers("однако цена 500")


def test_invented_options_are_dropped(session):
    """Регрессия 27.10: к вопросу «какие товары?» модель придумала кнопки с ценами, которых в базе не искала."""
    session.messages.append({"role": "user", "content": "Выстави счёт покупателю Альфа-Инжиниринг"})
    session.messages.append({"role": "tool", "tool_call_id": "t1",
                             "content": '{"items": [{"repr": "АЛЬФА-ИНЖИНИРИНГ ООО"}]}'})
    kept = session._grounded_options(["Кабель ВВГ, 100 м, 150 руб.", "Сервис по наладке оборудования, 10 ч, 2000 руб.",
                                      "Другое — уточню сам"])
    assert kept == ["Другое — уточню сам"]


def test_found_options_are_kept(session):
    session.messages.append({"role": "user", "content": "Сделай реализацию для Сидоренко"})
    session.messages.append({"role": "tool", "tool_call_id": "t1", "content": (
        '{"items": [{"repr": "Сидоренко Мария Олеговна ФЛ"}, {"repr": "Сидоренко Павел Андреевич ИП (Лотос)"}]}')})
    kept = session._grounded_options(["Сидоренко Мария Олеговна (ФЛ)", "Сидоренко Павел Андреевич, ИП «Лотос»",
                                      "Сидоренко Ольга Петровна"])
    assert kept == ["Сидоренко Мария Олеговна (ФЛ)", "Сидоренко Павел Андреевич, ИП «Лотос»"]


def test_option_with_unseen_price_is_dropped(session):
    session.messages.append({"role": "user", "content": "счёт на прошивку ККТ"})
    session.messages.append({"role": "tool", "tool_call_id": "t1",
                             "content": '{"items": [{"repr": "Услуга по обновлению прошивки ККТ"}]}'})
    assert session._grounded_options(["Услуга по обновлению прошивки ККТ"]) == ["Услуга по обновлению прошивки ККТ"]
    assert session._grounded_options(["Услуга по обновлению прошивки ККТ — 3000 руб."]) == []


def test_declined_names_in_options_are_kept(session):
    session.messages.append({"role": "user", "content": "реализация для Сидоренко"})
    session.messages.append({"role": "tool", "tool_call_id": "t1", "content": '{"items": [{"repr": "Сидоренко Мария Олеговна ФЛ"}]}'})
    assert session._grounded_options(["Марию Олеговну (ФЛ)"]) == ["Марию Олеговну (ФЛ)"]


def test_read_tool_times_out_fast(session, monkeypatch):
    """1С не отвечает на чтение — шаг завершается через READ_TOOL_TIMEOUT_SEC, а не через 15 минут."""
    from onec_agent import config
    monkeypatch.setattr(config, "READ_TOOL_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(config, "TOOL_TIMEOUT_SEC", 5)
    read = asyncio.run(session._run_client_tool("r1", {"type": "tool_call", "id": "r1", "needs_approval": False}))
    assert not read["ok"] and "не ответила" in read["error"] and not session.pending


SIDORENKO = json.dumps({"rows": [
    {"_ref": "Справочник.Контрагенты", "uuid": "11111111-0000-0000-0000-000000000001", "repr": "Сидоренко Павел Андреевич ИП «Лотос»"},
    {"_ref": "Справочник.Контрагенты", "uuid": "11111111-0000-0000-0000-000000000002", "repr": "Сидоренко Мария Олеговна"},
]}, ensure_ascii=False)
SALE = {"items": [{"item": "Услуга по обновлению прошивки ККТ", "quantity": 1, "price": 1500}]}


def _sidorenko_dialog(session, text):
    session.messages.append({"role": "user", "content": text})
    session.messages.append({"role": "tool", "tool_call_id": "c1", "content": SIDORENKO})


def test_ambiguous_counterparty_is_blocked(session):
    _sidorenko_dialog(session, "Сделай реализацию для Сидоренко на одну услугу по обновлению прошивки ККТ за 1500")
    who = {"_ref": "Справочник.Контрагенты", "uuid": "11111111-0000-0000-0000-000000000002", "repr": "Сидоренко Мария Олеговна"}
    missing = " ".join(session._missing_data("sale_create", {**SALE, "counterparty": who}))
    assert "какой именно контрагент" in missing and "Лотос" in missing


def test_distinguishing_word_resolves_counterparty(session):
    # «из Лотоса» отличает ИП от однофамилицы; имя в другом падеже («Марии») — тоже.
    _sidorenko_dialog(session, "Реализация для Сидоренко из Лотоса: одна услуга по обновлению прошивки ККТ за 1500")
    assert session._missing_data("sale_create", {**SALE, "counterparty": "Сидоренко Павел Андреевич ИП «Лотос»"}) == []
    session.messages.clear()
    _sidorenko_dialog(session, "Реализация для Сидоренко Марии: одна услуга по обновлению прошивки ККТ за 1500")
    who = {"_ref": "Справочник.Контрагенты", "uuid": "11111111-0000-0000-0000-000000000002", "repr": "Сидоренко Мария Олеговна"}
    assert session._missing_data("sale_create", {**SALE, "counterparty": who}) == []


def _found(*refs):
    return {"role": "tool", "tool_call_id": "f", "content": json.dumps({"items": list(refs)}, ensure_ascii=False)}


TWO_SIDORENKO = ({"_ref": "Справочник.Контрагенты", "uuid": "u1", "repr": "Сидоренко Павел Андреевич ИП «Лотос»"},
             {"_ref": "Справочник.Контрагенты", "uuid": "u2", "repr": "Сидоренко Мария Олеговна"})
INVOICES = ({"_ref": "Документ.СчетНаОплатуПокупателю", "uuid": "d1", "repr": "Счет на оплату 0000-000012 от 01.10.2026"},
            {"_ref": "Документ.СчетНаОплатуПокупателю", "uuid": "d2", "repr": "Счет на оплату 0000-000015 от 20.10.2026"})


def test_object_create_with_ambiguous_reference_is_blocked(session):
    session.messages.append({"role": "user", "content": "Создай акт сверки с Сидоренко за октябрь"})
    session.messages.append(_found(*TWO_SIDORENKO))
    args = {"full_name": "Документ.АктСверкиВзаиморасчетов", "fields": {"Контрагент": TWO_SIDORENKO[0]}}
    err = session._unconfirmed_numbers("object_create", args)
    assert err.startswith("Не создаю:") and "Мария Олеговна" in err and "options" in err
    session.messages[-2]["content"] = "Создай акт сверки с Сидоренко из Лотоса за октябрь"
    assert session._unconfirmed_numbers("object_create", args) is None


def test_object_update_of_ambiguous_document_is_blocked(session):
    session.messages.append({"role": "user", "content": "Поменяй дату счёта Альфа-Инжиниринг на 25.10.2026"})
    session.messages.append(_found(*INVOICES))
    args = {"ref": {"_ref": "Документ.СчетНаОплатуПокупателю", "uuid": "d1"}, "fields": {"Дата": "2026-10-25"}}
    err = session._unconfirmed_numbers("object_update", args)
    assert err.startswith("Не меняю:") and "какой именно объект" in err and "000015" in err
    session.messages[-2]["content"] = "Поменяй дату счёта 0000-000012 Альфа-Инжиниринг на 25.10.2026"
    assert session._unconfirmed_numbers("object_update", args) is None


def test_object_update_invented_number_is_blocked(session):
    session.messages.append({"role": "user", "content": "Поставь в счёте 12 комментарий «срочно»"})
    args = {"ref": {"_ref": "Документ.СчетНаОплатуПокупателю", "uuid": "d1"},
            "fields": {"Комментарий": "срочно", "СуммаДокумента": 15000}}
    assert "СуммаДокумента = 15000" in session._unconfirmed_numbers("object_update", args)


SALES = tuple({"_ref": "Документ.РеализацияТоваровУслуг", "uuid": f"s{i}", "repr": f"Реализация 0000-00000{i} от 0{i}.09.2026"}
              for i in range(1, 4))


def test_reading_found_documents_one_by_one_is_stopped(session):
    session.messages.append({"role": "user", "content": "Поставь комментарий «проверить» в реализацию для Альфа-Инжиниринг"})
    session.messages.append(_found(*SALES))
    session.turn_reads = 3
    assert session._reading_loop("object_get") is None
    session.turn_reads = 4
    stop = session._reading_loop("object_get")
    assert stop.startswith("Хватит") and "0000-000002" in stop and "ask_user" in stop
    assert session._reading_loop("object_find") is None


def test_object_find_hints_to_ask_when_action_needs_one_document(session):
    session.messages.append({"role": "user", "content": "Поставь комментарий «проверить» в реализацию для Альфа-Инжиниринг"})
    found = json.dumps({"items": [{**r, "date": f"2026-09-0{i}"} for i, r in enumerate(SALES, 1)]}, ensure_ascii=False)
    assert "ask_user" in json.loads(session._date_hint(found))["hint"]
    session.messages[-1]["content"] = "Покажи реализации Альфа-Инжиниринг"
    assert "hint" not in json.loads(session._date_hint(found))


def test_long_action_waits_by_its_own_timeout(session, monkeypatch):
    from onec_agent import config
    monkeypatch.setattr(config, "LONG_TOOL_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(config, "TOOL_TIMEOUT_SEC", 100)
    event = {"type": "tool_call", "id": "c1", "name": "month_close_run", "needs_approval": True, "long": True}
    res = asyncio.run(session._run_client_tool("c1", event))
    assert not res["ok"] and "ещё выполняется" in res["error"]


def test_third_identical_call_is_stopped(session):
    args = {"query": "проверка готовности к закрытию месяца"}
    assert session._repeated_call("kb_search", args) is None
    assert session._repeated_call("kb_search", args) is None
    stop = session._repeated_call("kb_search", args)
    assert stop.startswith("Хватит") and "ask_user" in stop
    assert session._repeated_call("kb_search", {"query": "другое"}) is None
    session.turn_calls = []  # новый ход — счётчик с нуля (start_turn обнуляет его)
    assert session._repeated_call("kb_search", args) is None


def test_second_identical_action_is_stopped(session):
    args = {"period": "2026-10"}
    assert session._repeated_call("month_close_run", args) is None
    stop = session._repeated_call("month_close_run", args)
    assert stop.startswith("Хватит") and "ошибки" in stop
    assert session._repeated_call("month_close_run", {"period": "2026-11"}) is None
