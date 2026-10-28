"""База знаний: поиск, стоп-слова, автоподстановка только к вопросам «почему / что делать»."""
from pathlib import Path

import pytest

from onec_agent import config, kb
from onec_agent.agent import KB_AUTO_TRIGGER


@pytest.fixture
def kb_dir():
    root = Path(config.KB_DIR)
    (root / "tickets").mkdir(parents=True)
    (root / "README.md").write_text("# README\nэтот файл в поиск не попадает: касса минус", "utf-8")
    (root / "tickets" / "kassa.md").write_text(
        "# Отрицательный остаток в кассе\n\nСимптом: кредитовое сальдо по счёту 50.\n\nРешение: карточка счёта.", "utf-8")
    (root / "tickets" / "uslugi.md").write_text(
        "# В реализации пропали услуги\n\nВид операции «Товары» очищает услуги при записи.", "utf-8")
    (root / "oshibki.md").write_text(
        "# Ошибки платформы\n\n## «Поле объекта не обнаружено»\nОбращение к свойству, которого нет.\n\n"
        "## Что делать при блокировке\nПовторить позже.", "utf-8")
    return root


def test_finds_relevant_ticket(kb_dir):
    top = kb.search("касса ушла в минус, что делать")["results"][0]
    assert top["source"] == "tickets/kassa.md" and top["kind"] == "решённое обращение"


def test_phrase_match_beats_common_words(kb_dir):
    q = "При проведении реализации ошибка: Поле объекта не обнаружено"
    assert kb.search(q)["results"][0]["source"].startswith("oshibki.md")


def test_kind_filter_and_readme_excluded(kb_dir):
    assert all(r["kind"] == "документ" for r in kb.search("ошибки платформы", kind="docs")["results"])
    assert not any("README" in r["source"] for r in kb.search("касса минус")["results"])


def test_index_refreshes_on_change(kb_dir):
    assert kb.search("инвентаризация")["total"] == 0
    (kb_dir / "inv.md").write_text("# Инвентаризация\n\nПорядок инвентаризации кассы.", "utf-8")
    assert kb.search("инвентаризация")["total"] == 1


def test_empty_kb_is_explained():
    assert "пуста" in kb.search("что угодно")["note"]


@pytest.mark.parametrize("question, auto", [
    ("У нас в кассе ушло в минус. Что делать?", True),
    ("При проведении вылетает ошибка {Документ.X(512)}", True),
    ("Можно ли поправить документы за сентябрь?", True),
    ("Сколько сейчас денег в кассе?", False),
    ("На какую сумму проведено поступлений в сентябре?", False),
])
def test_auto_kb_only_for_how_and_why(question, auto):
    assert bool(KB_AUTO_TRIGGER.search(question)) == auto
