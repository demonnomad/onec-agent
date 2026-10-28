"""Реестр инструментов и отбор групп под вопрос."""
import json

import pytest

from onec_agent.tools import BY_NAME, GROUPS, TOOLS, openai_tools


def test_schemas_are_valid_and_unique():
    assert len(BY_NAME) == len(TOOLS)
    for t in TOOLS:
        s = t.openai_schema()["function"]
        assert s["parameters"]["type"] == "object" and s["description"]
        json.dumps(s, ensure_ascii=False)
        assert isinstance(t.title({}), str)  # заголовок шага не падает на пустых аргументах


def test_every_tool_is_in_a_group():
    grouped = {n for names in GROUPS.values() for n in names}
    assert grouped == set(BY_NAME)


def test_selected_groups_are_smaller_than_all():
    full = len(json.dumps(openai_tools(), ensure_ascii=False))
    data = len(json.dumps(openai_tools({"data"}), ensure_ascii=False))
    assert data < full / 2
    names = {t["function"]["name"] for t in openai_tools({"data"})}
    assert {"query_run", "kb_search", "account_report"} <= names and "invoice_create" not in names


@pytest.mark.parametrize("question, groups", [
    ("Сколько проведено реализаций за сентябрь?", {"data"}),
    ("При проведении вылетает ошибка {Документ.Р.МодульОбъекта(512)}", {"diagnostics"}),
    ("Проведи реализацию ДС00-000002", {"actions"}),
    ("Найди контрагента Ромашка", {"data"}),  # ничего не совпало — «данные» по умолчанию
    ("Сколько НДС к уплате по декларации за 3 квартал?", {"data", "reporting"}),
    ("Сходится ли книга продаж с декларацией?", {"data", "reporting"}),
])
def test_group_picking(session, question, groups):
    assert session._pick_groups(question, None) == groups


def test_groups_carry_over_one_turn_and_files(session):
    session._pick_groups("Выстави счёт Ромашке", None)
    assert "actions" in session._pick_groups("да, на 10 штук", None)
    assert "files" in session._pick_groups("сверь", ["f1234abcd"])
    assert "files" in session._pick_groups("а теперь итог", None)  # файлы в диалоге остаются


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_titles_survive_wrong_argument_shapes(tool):
    """Модель присылает строки вместо ссылок и списков — заголовок шага не должен падать."""
    props = tool.parameters.get("properties", {})
    for value in ("Ромашка", 5, None, [], {}):
        tool.title({k: value for k in props})


def test_long_actions_are_marked():
    from onec_agent.tools import BY_NAME
    assert BY_NAME["month_close_run"].long and BY_NAME["month_close_run"].needs_approval
    assert not BY_NAME["document_post"].long


def test_closing_group_triggers_on_imperative():
    from onec_agent.agent import TOOL_GROUP_TRIGGERS
    for text in ("Закрой октябрь 2026", "Что мешает закрыть сентябрь?", "Закрытие месяца не прошло", "перепроведи документы"):
        assert TOOL_GROUP_TRIGGERS["closing"].search(text), text
