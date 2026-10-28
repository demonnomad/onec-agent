"""Код конфигурации по выгрузке: имена модулей, чтение, поиск."""
import json
from pathlib import Path

import pytest

from onec_agent import config, sources

MODULE = """Процедура ОбработкаПроведения(Отказ, РежимПроведения)
	Движения.Хозрасчетный.Записывать = Истина;
	ОбщийМодуль.Посчитать(ЭтотОбъект);
КонецПроцедуры

Функция СуммаДокумента() Экспорт
	Возврат 0;
КонецФункции
"""


@pytest.fixture
def dump(tmp_path):
    root = tmp_path / "bp-xml"
    (root / "Documents" / "Реализация" / "Ext").mkdir(parents=True)
    (root / "Documents" / "Реализация" / "Ext" / "ObjectModule.bsl").write_text(MODULE, "utf-8")
    (root / "Documents" / "Реализация" / "Forms" / "ФормаДокумента" / "Ext" / "Form").mkdir(parents=True)
    (root / "Documents" / "Реализация" / "Forms" / "ФормаДокумента" / "Ext" / "Form" / "Module.bsl").write_text(
        "Процедура ПриОткрытии(Отказ)\nКонецПроцедуры\n", "utf-8")
    (root / "CommonModules" / "ОбщегоНазначения" / "Ext").mkdir(parents=True)
    (root / "CommonModules" / "ОбщегоНазначения" / "Ext" / "Module.bsl").write_text(
        "// Проверка: Движения.Хозрасчетный\n", "utf-8")
    Path(config.SOURCES_FILE).write_text(json.dumps({"БП": [{"path": str(root), "label": "БП тест"}]}), "utf-8")
    sources._file_lists.clear()
    return root


@pytest.mark.parametrize("name", [
    "Документ.Реализация.МодульОбъекта",
    "Документ.Реализация.Форма.ФормаДокумента.Форма",
    "ОбщийМодуль.ОбщегоНазначения",
    "Документ.Реализация.Команда.Печать.МодульКоманды",
    "Конфигурация.МодульСеанса",
])
def test_module_name_roundtrip(name):
    assert sources.module_name(sources._module_relpath(name)) == name


def test_error_text_is_parsed():
    assert sources._parse_module_ref("{Документ.Реализация.МодульОбъекта(512)}") == ("Документ.Реализация.МодульОбъекта", 512)


def test_read_around_line_names_the_procedure(dump):
    r = sources.read("БП", "{Документ.Реализация.МодульОбъекта(2)}")
    assert r["inside"].startswith("Процедура ОбработкаПроведения") and ">>    2" in r["code"]


def test_read_procedure_and_list(dump):
    assert "Возврат 0" in sources.read("БП", "Документ.Реализация.МодульОбъекта", procedure="суммадокумента")["code"]
    assert sources.read("БП", "Документ.Реализация.МодульОбъекта")["methods"] == [
        "1: Процедура ОбработкаПроведения", "6: Функция СуммаДокумента Экспорт"]


def test_read_errors(dump):
    with pytest.raises(sources.SourceError, match="не найден"):
        sources.read("БП", "Документ.Нету.МодульОбъекта")
    with pytest.raises(sources.SourceError, match="всего 8 строк"):
        sources.read("БП", "Документ.Реализация.МодульОбъекта", line=500)


def test_search_exact_then_case_insensitive_and_scope(dump):
    r = sources.search("БП", "Движения.Хозрасчетный")
    assert r["total"] == 2 and r["modules"] == 2
    assert sources.search("БП", "движения.хозрасчетный")["total"] == 2
    assert sources.search("БП", "Движения.Хозрасчетный", scope="ОбщийМодуль")["total"] == 1


def test_no_sources_configured():
    with pytest.raises(sources.SourceError, match="не подключены"):
        sources.read("БП", "ОбщийМодуль.Х")
