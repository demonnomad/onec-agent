"""Проверка ответов в тестовом наборе: числа и правила поведения."""
from onec_agent.evals import answer_numbers, check, check_behavior


def test_numbers_with_spaces_and_comma():
    assert 12357215.27 in answer_numbers("на сумму 12 357 215,27 руб.")
    # Год рядом с числом не склеивается в одно число.
    assert answer_numbers("в сентябре 2026 67 документов") == [2026.0, 67.0]


def test_check_by_absolute_value_and_zero_words():
    assert check([-5840.67], "кредитовое сальдо 5 840,67")[0]
    assert check([0.0], "Дебетовое сальдо отсутствует — покупатели ничего не должны")[0]
    ok, missing = check([1294.0], "зарегистрировано 124 контрагента")
    assert not ok and missing == [1294.0]


def test_behavior_rules():
    checks = {"expect_any": ["posting_check"], "forbid_actions": True, "max_steps": 3}
    assert check_behavior(checks, ["object_find", "posting_check"], "ответ") == []
    fails = check_behavior(checks, ["object_find", "document_post"], "ответ")
    assert any("posting_check" in f for f in fails) and any("document_post" in f for f in fails)
    assert check_behavior({"max_steps": 1}, ["a", "b"], "") == ["шагов 2 при лимите 1"]


def test_behavior_answer_rules():
    assert check_behavior({"must_ask": True}, [], "Какие товары и цены?") == []
    assert check_behavior({"must_ask": True}, [], "Готово.") == ["нет уточняющего вопроса"]
    assert check_behavior({"answer_any": ["не могу", "нельзя"]}, [], "Удалить не могу") == []
    assert check_behavior({"answer_none": ["не заполнен"]}, [], "Реквизит не заполнен")


def test_suite_variants_need_detect(tmp_path, monkeypatch):
    import json
    from onec_agent import evals
    monkeypatch.setattr(evals, "EVALS_DIR", tmp_path)
    (tmp_path / "Конф.json").write_text(json.dumps({"cases": [{"id": "общий"}]}), "utf-8")
    (tmp_path / "Конф.demo.json").write_text(json.dumps({"detect": {"text": "ВЫБРАТЬ 1"}, "cases": []}), "utf-8")
    (tmp_path / "Конф.черновик.json").write_text(json.dumps({"cases": []}), "utf-8")  # без detect — не вариант
    assert [v["file"] for v in evals.suite_variants("Конф")] == ["Конф.demo.json"]
    assert evals.load_cases("Конф") == [{"id": "общий"}]
