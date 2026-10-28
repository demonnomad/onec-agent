"""Исходный код конфигурации: code_read / code_search по XML-выгрузке.

В сеансе 1С тексты модулей недоступны, поэтому код читаем из выгрузки «в файлы»
(Конфигуратор → Выгрузить конфигурацию в файлы, или ibcmd config export) — основной
конфигурации и расширений. Какие каталоги относятся к какой конфигурации — в
sources.json (см. sources.example.json): ключ — Метаданные.Имя из контекста базы.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

# Русское имя класса метаданных (как в тексте ошибки 1С) ↔ каталог выгрузки.
CLASS_DIRS = {
    "Справочник": "Catalogs", "Документ": "Documents", "Обработка": "DataProcessors", "Отчет": "Reports",
    "ОбщийМодуль": "CommonModules", "ОбщаяФорма": "CommonForms", "ОбщаяКоманда": "CommonCommands",
    "РегистрСведений": "InformationRegisters", "РегистрНакопления": "AccumulationRegisters",
    "РегистрБухгалтерии": "AccountingRegisters", "РегистрРасчета": "CalculationRegisters",
    "ПланСчетов": "ChartsOfAccounts", "ПланВидовХарактеристик": "ChartsOfCharacteristicTypes",
    "ПланВидовРасчета": "ChartsOfCalculationTypes", "ПланОбмена": "ExchangePlans", "Перечисление": "Enums",
    "ЖурналДокументов": "DocumentJournals", "БизнесПроцесс": "BusinessProcesses", "Задача": "Tasks",
    "Константа": "Constants", "HTTPСервис": "HTTPServices", "WebСервис": "WebServices",
}
DIR_CLASSES = {v: k for k, v in CLASS_DIRS.items()}
MODULE_FILES = {
    "МодульОбъекта": "ObjectModule.bsl", "МодульМенеджера": "ManagerModule.bsl",
    "МодульНабораЗаписей": "RecordSetModule.bsl", "МодульМенеджераЗначения": "ValueManagerModule.bsl",
    "Модуль": "Module.bsl",
}
FILE_MODULES = {v: k for k, v in MODULE_FILES.items()}
APP_MODULES = {
    "МодульУправляемогоПриложения": "ManagedApplicationModule.bsl", "МодульСеанса": "SessionModule.bsl",
    "МодульВнешнегоСоединения": "ExternalConnectionModule.bsl", "МодульОбычногоПриложения": "OrdinaryApplicationModule.bsl",
}

_LINE_IN_NAME = re.compile(r"\((\d+)\)\s*\}?\s*$")
_METHOD_START = re.compile(r"^\s*(?:Асинх\s+)?(Процедура|Функция)\s+([А-Яа-яЁёA-Za-z_0-9]+)\s*\(", re.IGNORECASE)
_METHOD_END = re.compile(r"^\s*(КонецПроцедуры|КонецФункции)\b", re.IGNORECASE)

READ_CONTEXT = 25      # строк вокруг указанной строки
METHOD_MAX_LINES = 160  # сколько строк процедуры отдаём за раз


class SourceError(Exception):
    pass


def _sources_file() -> Path:
    return Path(config.SOURCES_FILE)


def sources_for(config_name: str) -> list[dict]:
    path = _sources_file()
    if not path.is_file():
        raise SourceError("Исходники конфигурации не подключены: нет файла server/sources.json "
                          "(образец — sources.example.json). Код можно смотреть только по выгрузке конфигурации в файлы.")
    data = json.loads(path.read_text("utf-8"))
    found = [{"path": os.path.expanduser(s["path"]), "label": s.get("label") or Path(s["path"]).name}
             for s in data.get(config_name, [])]
    found = [s for s in found if Path(s["path"]).is_dir()]
    if not found:
        raise SourceError(f"Для конфигурации {config_name} в sources.json нет существующих каталогов выгрузки")
    return found


def _module_relpath(name: str) -> Path:
    """«Документ.Реализация.МодульОбъекта» → Documents/Реализация/Ext/ObjectModule.bsl и т.п."""
    parts = [p for p in name.split(".") if p]
    if len(parts) == 2 and parts[0] == "Конфигурация" and parts[1] in APP_MODULES:
        return Path("Ext") / APP_MODULES[parts[1]]
    if len(parts) < 2 or parts[0] not in CLASS_DIRS:
        raise SourceError(f"Не понимаю имя модуля «{name}». Примеры: Документ.РеализацияТоваровУслуг.МодульОбъекта, "
                          "ОбщийМодуль.ОбщегоНазначения, Документ.РеализацияТоваровУслуг.Форма.ФормаДокумента.Форма")
    cls, obj, rest = parts[0], parts[1], parts[2:]
    base = Path(CLASS_DIRS[cls]) / obj
    if cls == "ОбщийМодуль":
        return base / "Ext" / "Module.bsl"
    if cls == "ОбщаяФорма":
        return base / "Ext" / "Form" / "Module.bsl"
    if cls == "ОбщаяКоманда":
        return base / "Ext" / "CommandModule.bsl"
    if not rest:
        raise SourceError(f"Укажи модуль: {name}.МодульОбъекта, {name}.МодульМенеджера или {name}.Форма.<Имя>.Форма")
    if rest[0] == "Форма" and len(rest) >= 2:
        return base / "Forms" / rest[1] / "Ext" / "Form" / "Module.bsl"
    if rest[0] == "Команда" and len(rest) >= 2:
        return base / "Commands" / rest[1] / "Ext" / "CommandModule.bsl"
    if rest[0] in MODULE_FILES:
        return base / "Ext" / MODULE_FILES[rest[0]]
    raise SourceError(f"Неизвестный вид модуля «{rest[0]}» в «{name}»")


def module_name(rel: Path) -> str:
    """Обратное к _module_relpath: путь в выгрузке → имя модуля, как его пишет 1С."""
    p = rel.parts
    if len(p) == 2 and p[0] == "Ext":
        return "Конфигурация." + next((k for k, v in APP_MODULES.items() if v == p[1]), p[1])
    cls = DIR_CLASSES.get(p[0], p[0])
    if len(p) < 3:
        return ".".join(p)
    obj = p[1]
    if cls == "ОбщийМодуль":
        return f"ОбщийМодуль.{obj}"
    if cls == "ОбщаяФорма":
        return f"ОбщаяФорма.{obj}.Форма"
    if cls == "ОбщаяКоманда":
        return f"ОбщаяКоманда.{obj}.МодульКоманды"
    if p[2] == "Forms" and len(p) > 3:
        return f"{cls}.{obj}.Форма.{p[3]}.Форма"
    if p[2] == "Commands" and len(p) > 3:
        return f"{cls}.{obj}.Команда.{p[3]}.МодульКоманды"
    return f"{cls}.{obj}.{FILE_MODULES.get(p[-1], p[-1])}"


def _parse_module_ref(text: str) -> tuple[str, int | None]:
    """Принимает и голое имя, и фрагмент текста ошибки: «{Документ.X.МодульОбъекта(512)}»."""
    text = text.strip().strip("{}").strip()
    m = _LINE_IN_NAME.search(text)
    line = int(m.group(1)) if m else None
    if m:
        text = text[: m.start()]
    return text.strip().strip("{}").strip(), line


def _methods(lines: list[str]) -> list[dict]:
    found = []
    for i, line in enumerate(lines, 1):
        m = _METHOD_START.match(line)
        if m:
            found.append({"line": i, "kind": m.group(1), "name": m.group(2),
                          "export": "экспорт" in line.lower()})
    return found


def _numbered(lines: list[str], start: int, end: int, mark: int | None = None) -> str:
    out = []
    for n in range(max(1, start), min(len(lines), end) + 1):
        prefix = ">>" if n == mark else "  "
        out.append(f"{prefix}{n:5d}  {lines[n - 1].rstrip()}")
    return "\n".join(out)


def read(config_name: str, module: str, line: int | None = None, procedure: str | None = None,
         start: int | None = None) -> dict:
    name, line_in_name = _parse_module_ref(module)
    line = line or line_in_name
    rel = _module_relpath(name)
    sources = sources_for(config_name)
    hits = [s for s in sources if (Path(s["path"]) / rel).is_file()]
    if not hits:
        raise SourceError(f"Модуль {name} не найден в выгрузке ({', '.join(s['label'] for s in sources)}). "
                          "Проверь имя через metadata_search или найди место code_search.")
    src = hits[0]
    text = (Path(src["path"]) / rel).read_text("utf-8-sig", errors="replace")
    lines = text.splitlines()
    methods = _methods(lines)
    result = {"module": name, "source": src["label"], "lines_total": len(lines)}
    if len(hits) > 1:
        result["also_in"] = [s["label"] for s in hits[1:]]

    if procedure:
        target = next((m for m in methods if m["name"].lower() == procedure.lower()), None)
        if target is None:
            raise SourceError(f"В {name} нет процедуры или функции «{procedure}». Есть: "
                              + ", ".join(m["name"] for m in methods[:60]))
        first = start or target["line"]
        end = first
        while end < len(lines) and not _METHOD_END.match(lines[end - 1]):
            end += 1
        last = min(end, first + METHOD_MAX_LINES - 1)
        result["code"] = _numbered(lines, first, last)
        if last < end:
            result["more"] = f"показаны строки {first}–{last} из {target['line']}–{end}: продолжение — start={last + 1}"
        return result

    if line:
        if line > len(lines):
            raise SourceError(f"В {name} всего {len(lines)} строк — строки {line} нет (выгрузка другой версии?)")
        owner = next((m for m in reversed(methods) if m["line"] <= line), None)
        if owner:
            result["inside"] = f"{owner['kind']} {owner['name']} (строка {owner['line']})"
        result["code"] = _numbered(lines, line - READ_CONTEXT, line + READ_CONTEXT, mark=line)
        return result

    result["methods"] = [f"{m['line']}: {m['kind']} {m['name']}{' Экспорт' if m['export'] else ''}"
                         for m in methods[:300]]
    if len(methods) > 300:
        result["methods"].append(f"… всего {len(methods)}")
    return result


_file_lists: dict[str, list[Path]] = {}


def _bsl_files(root: str) -> list[Path]:
    if root not in _file_lists:
        _file_lists[root] = [Path(d) / f for d, _, fs in os.walk(root) for f in fs if f.endswith(".bsl")]
    return _file_lists[root]


def _scope_prefix(scope: str | None) -> tuple[str, ...]:
    if not scope:
        return ()
    parts = [p for p in scope.split(".") if p]
    if parts[0] not in CLASS_DIRS:
        raise SourceError(f"scope: начни с вида объекта — {', '.join(list(CLASS_DIRS)[:8])}…")
    return tuple([CLASS_DIRS[parts[0]]] + parts[1:2])


def search(config_name: str, text: str, scope: str | None = None, limit: int = 40) -> dict:
    text = (text or "").strip()
    if len(text) < 3:
        raise SourceError("Слишком короткая строка поиска — нужно хотя бы 3 символа")
    prefix = _scope_prefix(scope)
    sources = sources_for(config_name)

    def candidates():
        for s in sources:
            root = Path(s["path"])
            for f in _bsl_files(s["path"]):
                rel = f.relative_to(root)
                if rel.parts[: len(prefix)] == prefix:
                    yield s, root, rel

    files = list(candidates())
    needle = text.encode("utf-8")
    pattern = re.compile(re.escape(text), re.IGNORECASE)

    def scan(item, exact: bool):
        s, root, rel = item
        data = (root / rel).read_bytes()
        if exact and needle not in data:
            return []
        body = data.decode("utf-8-sig", errors="replace")
        if not exact and not pattern.search(body):
            return []
        out = []
        for n, line in enumerate(body.splitlines(), 1):
            if (text in line) if exact else pattern.search(line):
                out.append((s["label"], module_name(rel), n, line.strip()[:220]))
        return out

    # Сначала точное совпадение (быстро, по байтам), без учёта регистра — только если пусто.
    matches = []
    for exact in (True, False):
        with ThreadPoolExecutor(8) as pool:
            for found in pool.map(lambda it: scan(it, exact), files):
                matches.extend(found)
        if matches:
            break
    matches.sort(key=lambda m: (m[0] != sources[0]["label"], m[1], m[2]))
    modules = sorted({m[1] for m in matches})
    return {
        "query": text, "scope": scope or "вся конфигурация", "total": len(matches), "modules": len(modules),
        "matches": [f"{m[1]}({m[2]}): {m[3]}" + (f"  [{m[0]}]" if m[0] != sources[0]["label"] else "")
                    for m in matches[:limit]],
        **({"more": f"показаны {limit} из {len(matches)} — сузь scope"} if len(matches) > limit else {}),
    }
