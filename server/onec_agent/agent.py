"""Цикл агента: LLM ↔ инструменты, с потоковыми событиями наружу.

Сессия не знает про WebSocket: она получает функцию emit(event) и
принимает результаты инструментов через resolve_tool(). Так тот же цикл
можно будет повесить на другой транспорт (MCP, бот, тесты).
"""
import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from . import audit, config, files, kb, providers, settings, sources
from .tools import BY_NAME, openai_tools

log = logging.getLogger("onec_agent.agent")

Emit = Callable[[dict], Awaitable[None]]

SYSTEM_PROMPT = """Ты — ИИ-консультант, встроенный в информационную базу 1С:Предприятие. Работаешь как агент:
сам изучаешь базу инструментами и доводишь задачу до ответа. Пользователь видит каждый твой шаг.

Контекст базы:
{context}

Как работать:
- Не выдумывай имена объектов и полей. Сначала metadata_search → metadata_describe, потом запрос.
- Документы и элементы справочников ищи через object_find; ссылки из ответов инструментов передавай дальше как есть.
- «Почему не проводится документ» — object_find, затем posting_check (пробное проведение с откатом): он
  вернёт настоящие сообщения 1С. Про ошибки и сбои — eventlog_search.
- Вопрос про сами документы («сколько выдано/проведено», «на какую сумму», «последний») — реализации,
  поступления, счета-фактуры, счета, платёжки — запрос к таблице документа (КОЛИЧЕСТВО(*),
  СУММА(Т.СуммаДокумента)), а не обороты счетов. Число элементов справочника —
  тоже запросом КОЛИЧЕСТВО(*), не object_find (он отдаёт только первые строки).
- Остатки и обороты (касса, банк, долги, выручка, склад) — register_balance / register_turnover, а не
  query_run: там не нужно писать запрос. Коды счетов: касса 50, расчётный счёт 51, покупатели 62,
  поставщики 60, выручка 90.01. Итоги бери из totals.
  Сальдо в totals уже развёрнутое. По 62: Дт — долг покупателей нам, Кт — полученные от них авансы.
  По 60: Кт — наш долг поставщикам, Дт — выданные им авансы. Не путай стороны: Кт по 62 — это не долг покупателей.
- Документ «на основании» другого (счёт-фактура на реализацию, реализация по счёту) — create_based_on.
- Провести или отменить проведение существующего документа — document_post / document_unpost.
  Исправить реквизит существующего объекта (дату, договор, комментарий…) — object_update.
- ОСВ, «откуда сумма на счёте», движения по контрагенту, акт сверки — account_report (osv / analysis / card).
  «Кто больше всех должен», разбивка счёта по контрагентам — account_report analysis: строки уже
  отсортированы по сальдо Дт−Кт по убыванию. «Сколько проводок / операций по счёту» — account_report card,
  число — totals.Проводок.
  Другие отчёты конфигурации — report_run.
- Отчётность. Декларация (НДС, УСН, 6-НДФЛ, РСВ) и бухотчётность, сохранённые в базе: regulated_report kind=list,
  затем kind=read — числа строк бери оттуда (раздел, строка, графа, название строки). Книги продаж и покупок —
  vat_book. «Сходится ли декларация с книгами / учётом», «откуда расхождение» — vat_reconcile за квартал.
  В декларации суммы в рублях без копеек: расхождение с книгой до 1 руб. — округление, не ошибка.
  НДС у этой организации может быть по ставкам 5% / 7% (УСН) — бери ставки из книги, не подставляй 20%.
- Ошибка с местом в коде вида {{Документ.X.МодульОбъекта(512)}} — code_read с этим текстом; где что-то
  вызывается или откуда берётся сообщение — code_search. Поведение не как в типовой — extensions_list.
  Текст ошибки платформы («Поле объекта не обнаружено», «Метод объекта не обнаружен», «Конфликт блокировок»…)
  — сначала kb_search по нему: там сказано, что ошибка означает. Не придумывай причин, которых нет в
  найденном и в прочитанном коде; если точной причины не видно — скажи, что ещё нужно (полный текст ошибки,
  документ, на котором она возникает).
  Не объясняй код, которого не читал: смысл параметров и что делает вызываемая процедура — сначала
  code_read этой процедуры (module + procedure). «Поле объекта не обнаружено» / «Метод объекта не
  обнаружен» — частая причина: расширение отстало от обновлённой конфигурации; проверь extensions_list
  с object = объект из ошибки. Вини расширение, только если object_verdict говорит «ИЗМЕНЁН».
- Вопросы «как сделать», «что делать, если», «почему так» — сначала kb_search (инструкции компании и
  решённые обращения); если найдено — опирайся на найденное и назови источник.
- Показать пользователю список с отбором — open_list.
- Запросы пиши на языке запросов 1С: таблица всегда с псевдонимом (ИЗ Документ.Имя КАК Т), поля через Т.Поле,
  ссылки через ПРЕДСТАВЛЕНИЕ(Т.Поле). Даты — только параметрами (&Начало), значения передавай в params
  строкой ISO. Образец:
    text:   ВЫБРАТЬ КОЛИЧЕСТВО(*) КАК Количество, СУММА(Т.СуммаДокумента) КАК Сумма
            ИЗ Документ.СчетФактураВыданный КАК Т
            ГДЕ Т.Проведен И Т.Дата МЕЖДУ &Начало И &Конец
    params: {{"Начало": "2026-09-01", "Конец": "2026-09-30T23:59:59"}}
- Значения перечислений — только ЗНАЧЕНИЕ(Перечисление.Имя.Значение) прямо в тексте запроса; точное имя
  значения бери из metadata_describe (не передавай перечисление строкой в params).
- Сортировка: УПОРЯДОЧИТЬ ПО Т.Дата УБЫВ; «последний» — ВЫБРАТЬ ПЕРВЫЕ 1 … УПОРЯДОЧИТЬ ПО Т.Дата УБЫВ.
  Вложенный запрос в условии через «=» в 1С нельзя. ПЕРВЫЕ не смешивай с агрегатами.
- Количество и суммы ВСЕГДА считай агрегатным запросом (КОЛИЧЕСТВО(*), СУММА(...)).
  СГРУППИРОВАТЬ ПО добавляй, только когда нужна разбивка, и всегда с полями после него.
  Никогда не складывай строки выборки сам: выборка может быть обрезана.
- Учитывай только проведённые и не помеченные на удаление документы (Т.Проведен), если не просят иное.
- Говори только то, что видно в результатах инструментов. Нет данных — так и скажи, не додумывай.
- Ошибка запроса — это НЕ ноль и НЕ «данных нет». По ошибкам никаких выводов о данных не делай.
- Прежде чем утверждать «этого нет в базе», проверь и другие подходящие объекты (например, и выданные,
  и полученные счета-фактуры) и скажи, где именно искал.
- Если инструмент вернул ошибку — прочитай её, исправь вызов и попробуй снова (не больше 2 раз подряд).
  Если не получается — остановись и честно скажи пользователю, что именно не вышло.
- Данные меняют только: invoice_create (счёт покупателю), sale_create (реализация), purchase_create
  (поступление товаров и услуг), create_based_on (ввод на основании), document_post / document_unpost, object_update и
  object_create (любой другой документ — черновиком — или элемент справочника). Каждое такое действие
  пользователь подтверждает в окне 1С. Для этих трёх документов всегда бери специальный инструмент, не object_create.
  Всё остальное — только чтение; ui_open лишь открывает форму.
- Создание документа — в два шага. Сначала собери от пользователя: кто контрагент, какие именно товары
  или услуги, сколько и по какой цене. Чего он не назвал — НЕ подставляй (ни цену, ни количество «1»,
  ни «похожую» номенклатуру из базы): вызови ask_user и спроси одним вопросом всё недостающее; найденные
  в базе варианты (контрагенты, номенклатура) дай в options. Несколько подходящих контрагентов или позиций —
  тоже ask_user с вариантами. Только когда всё названо — вызывай инструмент создания.
  Количество словами («одна», «две», «пара») — это число, не переспрашивай.
  НЕ спрашивай то, что заполнит 1С: ставку и сумму НДС, счета учёта, организацию, склад, договор
  (если у контрагента он один) — для документа достаточно контрагента, позиций, количества и цены.
- Перед созданием прочего объекта: metadata_describe, обязательные реквизиты — тем, что назвал пользователь.
- Если 1С вернула «несколько подходящих» или «не найдено» — покажи варианты пользователю и уточни.
- После создания сообщи, что создано и что пользователь выбрал (outcome), и что осталось незаполненным.
- Пользователь может прикрепить файлы (скриншоты, Excel, PDF, Word, текст). В сообщении видно начало файла;
  остальное читай file_read по частям, конкретные строки ищи file_search. Сверяя файл с базой, бери
  значения из файла (номера, ИНН, суммы) и проверяй их запросами к базе. Скриншот уже переведён в текст.
- Если вопрос неоднозначен — задай один короткий уточняющий вопрос.
- Номер вида «ДС00-000003», «0000-000045» — номер документа: ищи object_find по нужному документу, не среди контрагентов.
- Удалять и помечать на удаление объекты ты не можешь — так и скажи; пользователь делает это сам
  (можешь открыть ему список через open_list).

Ответ: по-русски, кратко и по делу, с конкретикой из базы (номера, даты, суммы). Markdown разрешён.
Не пиши рассуждения и планы между вызовами инструментов («Проверим…», «Попробуем…») — пользователь и так
видит каждый шаг. Текст пиши только в итоговом ответе или когда нужно что-то спросить у пользователя.
{notes}"""

GOODS_TOOLS = {"invoice_create", "sale_create", "purchase_create"}
DATA_GUARD_TOOLS = GOODS_TOOLS | {"object_create"}
# Слова, по которым нельзя считать, что пользователь назвал объект: «ООО», «ИП», «услуга»…
GENERIC_STEMS = {"ооо", "оао", "зао", "пао", "ао", "ип", "услуг", "товар", "работ", "по", "на", "для", "из",
                 "от", "и", "в", "с", "шт"}
_WORDS = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{2,}")
# Служебные варианты ответа, которые не утверждают ничего о данных базы.
META_OPTION_STEMS = {"друго", "уточн", "отмен", "нет", "да", "все", "оба", "ни", "одни", "сам", "вручн", "введу",
                     "укажу", "позже", "не", "знаю", "вариа", "свой"}


def _stems(text: str, size: int = 5) -> set[str]:
    """Грубые основы слов (первые 5 букв) — «прошивки» и «прошивка» совпадают."""
    return {w.lower().replace("ё", "е")[:size] for w in _WORDS.findall(text or "")}


_REF_OBJECT = re.compile(r'\{[^{}]*"_ref"[^{}]*\}')


def _refs_in(text: str, full_name: str) -> list[dict]:
    """Ссылки {"_ref", "uuid", "repr"} нужного вида из результата инструмента (без разбора всего JSON:
    результат бывает обрезан)."""
    found = []
    for raw in _REF_OBJECT.findall(text or ""):
        try:
            ref = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ref.get("_ref") == full_name and ref.get("uuid") and ref not in found:
            found.append(ref)
    return found


def _label(value) -> str:
    return value.get("repr", "?") if isinstance(value, dict) else str(value or "?")
_NUM = re.compile(r"(?<![\w.])\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?")
_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


# Количество словами: «одна услуга», «две штуки», «пара».
NUMBER_WORDS = {"один": 1, "одна": 1, "одно": 1, "одну": 1, "два": 2, "две": 2, "пара": 2, "пару": 2, "три": 3,
                "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}


def _numbers(text: str) -> set[float]:
    found = {float(v) for w, v in NUMBER_WORDS.items() if re.search(rf"(?<![а-яё]){w}(?![а-яё])", (text or "").lower())}
    for raw in _NUM.findall(text or ""):
        try:
            found.add(float(raw.replace(" ", "").replace("\u00a0", "").replace(",", ".")))
        except ValueError:
            pass
    return found


# Ошибок инструментов подряд, после которых агенту велят остановиться.
MAX_ERRORS_IN_ROW = 3
# Подсказки к частым ошибкам запросов: модель повторяет тот же запрос, если не сказать, как чинить.
QUERY_ERROR_HINTS = [
    ("не входит в группу", "Агрегат (СУММА, КОЛИЧЕСТВО) и поле без группировки в одном запросе. Для «последнего / "
     "самого большого документа» убери агрегаты: ВЫБРАТЬ ПЕРВЫЕ 1 Т.СуммаДокумента, Т.Дата ИЗ … УПОРЯДОЧИТЬ ПО Т.Дата УБЫВ. "
     "Для итога — убери УПОРЯДОЧИТЬ ПО и ПЕРВЫЕ."),
    ("Поле не найдено", "Такого поля нет: посмотри точные имена через metadata_describe."),
    ("Таблица не найдена", "Такой таблицы нет: найди точное имя объекта через metadata_search."),
]

# Какие группы инструментов (tools.GROUPS) нужны вопросу — по словам. Ничего не совпало — «data».
TOOL_GROUP_TRIGGERS = {
    "data": re.compile(r"сколько|сумм|остат|оборот|сальдо|выручк|долг|должн|задолж|осв|оборотно|карточк|"
                       r"отч[её]т|анализ|движени|проводк|итог|касс|банк|расч[её]тн|сч[её]т[а-я]* \d|\b\d{2}(?:\.\d{2})?\b|"
                       r"поступил|продаж|покупател|поставщик|ндс|деньг|денег", re.IGNORECASE),
    "diagnostics": re.compile(r"почему|ошибк|не провод|не работа|не заполня|сбо[йи]|пада|вылета|код|модул|"
                              r"процедур|функци|расширени|журнал|не так|странн|\{[^}]*\(\d+\)\}", re.IGNORECASE),
    "reporting": re.compile(r"деклараци|отч[её]тност|регламент|книг[аиуе] (продаж|покупок)|сверк|ндс|баланс|"
                            r"финансов\w* результат|6-?ндфл|рсв|страхов\w* взнос|усн|к уплате|к возмещению|"
                            r"строк[аеиу] \d{3}|раздел[а-я]* \d", re.IGNORECASE),
    "actions": re.compile(r"созда|выстав|оформ|сдела|проведи|провести|перепровед|отмени|измени|помен|исправь|замени|"
                          r"открой|покажи список|на основании|запиши|добав|завед|пометь|выпиши", re.IGNORECASE),
}

# С какой оценки BM25 совпадение в базе знаний подкладывается к вопросу автоматически.
KB_AUTO_MIN_SCORE = 4.0
KB_AUTO_TRIGGER = re.compile(
    r"почему|зачем|как (?:сделать|оформить|исправить|настроить|найти|быть|правильно)|что делать|что значит|"
    r"ошибк|не (?:провод|заполня|работа|открыва|записыва|формиру|выгружа|загружа)|пропал|исчез|минус|"
    r"можно ли|нельзя|запрет|[«{]", re.IGNORECASE)

_ISO_LITERAL = re.compile(r"'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?'")


def _fix_date_literals(text: str) -> str:
    """'2026-09-01' в тексте запроса → ДАТАВРЕМЯ(2026, 9, 1): локальные модели упорно пишут
    даты как в SQL, а язык запросов 1С такие литералы не понимает."""
    def repl(m: re.Match) -> str:
        parts = [int(x) for x in m.groups() if x is not None]
        if len(parts) > 3:
            parts += [0] * (6 - len(parts))  # ДАТАВРЕМЯ принимает 3 или 6 аргументов
        return "ДАТАВРЕМЯ(" + ", ".join(map(str, parts)) + ")"
    return _ISO_LITERAL.sub(repl, text)


_DANGLING_CLAUSE = re.compile(r"\s*(СГРУППИРОВАТЬ|УПОРЯДОЧИТЬ)\s+ПО\s*;?\s*$", re.IGNORECASE)


_TYPOS = [
    (re.compile(r"(?<![А-Яа-яЁё])ГРУППИРОВАТЬ(?=\s+ПО)", re.IGNORECASE), "СГРУППИРОВАТЬ"),
    (re.compile(r"(?<![А-Яа-яЁё])ПОРЯДОЧИТЬ(?=\s+ПО)", re.IGNORECASE), "УПОРЯДОЧИТЬ"),
    (re.compile(r"(?<![А-Яа-яЁё])ПОРЯДОК(?=\s+ПО)", re.IGNORECASE), "УПОРЯДОЧИТЬ"),
    (re.compile(r"(?<![А-Яа-яЁё])(ПОЗДНЕЕ|DESC)(?![А-Яа-яЁё\w])", re.IGNORECASE), "УБЫВ"),
    (re.compile(r"\.Удален(?![А-Яа-яЁё])"), ".ПометкаУдаления"),
    (re.compile(r"(=\s*)НЕТ(?![А-Яа-яЁё\w])", re.IGNORECASE), r"\1ЛОЖЬ"),
    (re.compile(r"(=\s*)ДА(?![А-Яа-яЁё\w])", re.IGNORECASE), r"\1ИСТИНА"),
]


_ENUM_VALUE = re.compile(r"(ЗНАЧЕНИЕ\s*\(\s*Перечисление\.[А-Яа-яЁё\w]+\.)([^)]*?)(\s*\))", re.IGNORECASE)


def _fix_enum_values(text: str) -> str:
    """ЗНАЧЕНИЕ(Перечисление.Вид.На аванс) → …НаАванс: модель берёт синоним значения вместо имени."""
    def repl(m: re.Match) -> str:
        words = m.group(2).split()
        if len(words) < 2:
            return m.group(0)
        return m.group(1) + "".join(w[:1].upper() + w[1:] for w in words) + m.group(3)
    return _ENUM_VALUE.sub(repl, text)


def _fix_query(text: str) -> str:
    """Детерминированно чиним частые ошибки локальных моделей в тексте запроса."""
    text = _fix_enum_values(_fix_date_literals(text))
    for pattern, repl in _TYPOS:
        text = pattern.sub(repl, text)
    return _DANGLING_CLAUSE.sub("", text)


def _base_notes(ctx: dict) -> str:
    """Памятка базы (аналог CLAUDE.md): notes/<ИмяКонфигурации>.md, если есть."""
    name = ctx.get("config_name")
    if not name:
        return ""
    path = Path(config.NOTES_DIR) / f"{name}.md"
    if not path.is_file():
        return ""
    return "\nПамятка по этой конфигурации:\n" + path.read_text("utf-8").strip()


def _mark_truncated(tool: str, text: str) -> str:
    """Явно предупреждает модель, что выборка неполная: иначе она «досчитывает» итоги сама."""
    if tool != "query_run":
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    shown, total = len(data.get("rows") or []), data.get("total") or 0
    if total > shown:
        data["warning"] = (f"Показано {shown} строк из {total}. Итоги по этим строкам НЕ считай — "
                           "для количества и сумм выполни агрегатный запрос.")
        return json.dumps(data, ensure_ascii=False)
    return text


def _parse_args(call: dict) -> dict:
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def _safe_title(tool, args: dict) -> str:
    """Заголовок шага для интерфейса. Модель может прислать аргументы не той формы (строку вместо
    ссылки) — заголовок от этого не должен ронять ход агента."""
    try:
        return tool.title(args)
    except Exception:  # noqa: BLE001 — любой сбой заголовка заменяем именем инструмента
        return tool.name


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def _explain_totals(tool: str, text: str) -> str:
    """Итог бухрегистра словами первой строкой: локальные модели путают Дт и Кт, читая голый JSON."""
    if tool not in ("register_balance", "register_turnover", "account_report"):
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    data.pop("query", None)  # текст запроса виден пользователю в шаге, модели он только мешает
    # Строки с подписанными полями: в голом массиве [имя, 0, 953881.17] модель путает, где Дт, а где Кт.
    columns, rows = data.get("columns"), data.get("rows")
    if isinstance(columns, list) and isinstance(rows, list) and rows and isinstance(rows[0], list):
        # Ссылки — наименованиями: в отчёте модели нужны имена, uuid только раздувают ответ.
        named = lambda v: v.get("repr", "") if isinstance(v, dict) and "_ref" in v else v
        data["rows"] = [{c: named(v) for c, v in zip(columns, r)} for r in rows]
        data.pop("columns")
    if tool == "account_report":
        head = ""
        if data.get("report", "").startswith("Анализ") and data.get("rows"):
            first = data["rows"][0]
            label = next((v for k, v in first.items() if not k.startswith("Сумма")), "")
            head = (f"Строки отсортированы по конечному сальдо Дт−Кт по убыванию. Наибольшее: {label} — "
                    f"конечное сальдо Дт {_money(float(first.get('СуммаКонечныйОстатокДт') or 0))}, "
                    f"Кт {_money(float(first.get('СуммаКонечныйОстатокКт') or 0))}.\n")
        return head + json.dumps(data, ensure_ascii=False)
    totals = data.get("totals") or {}
    kind = "Остаток" if tool == "register_balance" else "Оборот"
    dt_key, kt_key = f"Сумма{kind}Дт", f"Сумма{kind}Кт"
    if dt_key not in totals and kt_key not in totals:
        return json.dumps(data, ensure_ascii=False)
    dt_sum, kt_sum = float(totals.get(dt_key) or 0), float(totals.get(kt_key) or 0)
    if kind == "Остаток":
        side = "дебетовое" if dt_sum >= kt_sum else "кредитовое"
        line = (f"ИТОГ {data.get('period', '')}: сальдо по Дт = {_money(dt_sum)}, сальдо по Кт = {_money(kt_sum)}. "
                f"Свёрнуто: {side} сальдо {_money(abs(dt_sum - kt_sum))}.")
        rows = data.get("rows") or []
        if len(rows) > 1 and isinstance(rows[0], dict):
            first = rows[0]
            label = next((v for k, v in first.items() if not k.startswith("Сумма")), "")
            line += (f"\nСтроки отсортированы по сальдо Дт−Кт по убыванию. Наибольшее дебетовое: {label} — "
                     f"Дт {_money(float(first.get(dt_key) or 0))}, Кт {_money(float(first.get(kt_key) or 0))}.")
    else:
        line = f"ИТОГ {data.get('period', '')}: оборот по Дт = {_money(dt_sum)}, оборот по Кт = {_money(kt_sum)}."
    return line + "\n" + json.dumps(data, ensure_ascii=False)


def _format_context(ctx: dict) -> str:
    rows = {
        "Конфигурация": f"{ctx.get('config', '?')} {ctx.get('config_version', '')}".strip(),
        "Платформа": ctx.get("platform", "?"),
        "Пользователь": ctx.get("user", "?"),
        "Клиент": ctx.get("client", "?"),
        "Сегодня": ctx.get("today") or dt.date.today().isoformat(),
    }
    return "\n".join(f"- {k}: {v}" for k, v in rows.items())


class AgentSession:
    def __init__(self, emit: Emit, http: aiohttp.ClientSession):
        self.emit = emit
        self.http = http
        self.context: dict = {}
        self.messages: list[dict] = []
        self.pending: dict[str, asyncio.Future] = {}
        self.turn: asyncio.Task | None = None
        self.eval_task: asyncio.Task | None = None  # прогон тестового набора (/тесты)
        self.profile: dict = providers.default_profile()
        self.client = providers.Client(self.profile)
        self.stop_requested = False
        self.dry_run_tools: set[str] = set()  # в тестовом прогоне эти инструменты не выполняются
        self.session_id = ""  # идентификатор диалога со стороны страницы — к нему привязаны вложения

    def set_context(self, ctx: dict) -> None:
        self.context = ctx or {}
        self.apply_profile(settings.profile_for(self.base_id))
        self.reset()

    @property
    def base_id(self) -> str:
        return self.context.get("base_id") or self.context.get("config_name") or "default"

    def apply_profile(self, profile: dict) -> None:
        self.profile = profile
        self.client = providers.Client(profile)

    def reset(self) -> None:
        prompt = SYSTEM_PROMPT.format(context=_format_context(self.context), notes=_base_notes(self.context))
        self.messages = [{"role": "system", "content": prompt}]
        self.prev_groups: set[str] = set()
        self.tool_groups: set[str] | None = None
        self.has_files = False
        self.errors_in_row = 0
        self.last_error = ""

    @property
    def busy(self) -> bool:
        return any(t is not None and not t.done() for t in (self.turn, self.eval_task))

    def _pick_groups(self, text: str, attachments: list[str] | None) -> set[str]:
        groups = {g for g, rx in TOOL_GROUP_TRIGGERS.items() if rx.search(text)} or {"data"}
        if attachments or self.has_files:
            groups.add("files")
            self.has_files = True
        # Группы прошлого хода остаются: «а теперь проведи его» продолжает разговор о том же.
        current, self.prev_groups = groups | self.prev_groups, groups
        return current

    def start_turn(self, text: str, attachments: list[str] | None = None) -> None:
        self.kb_query = text
        self.tool_groups = self._pick_groups(text, attachments)
        if attachments and self.session_id:
            text += files.attachment_note(self.session_id, attachments)
        self.messages.append({"role": "user", "content": text})
        self.turn = asyncio.create_task(self._run_turn())

    def interrupt(self) -> None:
        # Ход агента перехватывает отмену и завершается штатно — прогон тестов, который его ждёт,
        # об отмене иначе не узнает; флаг он проверяет после каждого хода.
        self.stop_requested = True
        for task in (self.eval_task, self.turn):
            if task is not None and not task.done():
                task.cancel()

    def resolve_tool(self, call_id: str, payload: dict) -> None:
        fut = self.pending.get(call_id)
        if fut and not fut.done():
            fut.set_result(payload)

    async def _auto_kb(self) -> None:
        """RAG без участия модели: локальная модель часто не догадывается спросить базу знаний,
        поэтому уверенные совпадения по вопросу подкладываем сами — шагом, который видит пользователь."""
        query, self.kb_query = getattr(self, "kb_query", ""), ""
        # Только вопросы «как / почему / что делать / ошибка»: к вопросам о данных («сколько», «на какую
        # сумму») похожие по словам обращения подмешивают шум и уводят модель не туда.
        if not query or query.startswith("/") or not KB_AUTO_TRIGGER.search(query):
            return
        found = await asyncio.to_thread(kb.search, query, None, 3)
        results = found.get("results") or []
        if not results or results[0]["score"] < KB_AUTO_MIN_SCORE:
            return
        top = results[0]["score"]
        picked = [r for r in results if r["score"] >= max(KB_AUTO_MIN_SCORE, 0.6 * top)][:2]
        call_id = f"auto-kb-{len(self.messages)}"
        summary = "; ".join(r["title"] for r in picked)
        await self.emit({"type": "tool_call", "id": call_id, "name": "kb_search", "args": {"query": query[:200]},
                         "title": "Ищу в базе знаний похожее", "kind": "read", "needs_approval": False, "executor": "server"})
        await self.emit({"type": "tool_result", "id": call_id, "ok": True, "summary": summary,
                         "result": json.dumps({"results": picked}, ensure_ascii=False)})
        note = "\n\n".join(f"[{r['kind']}: {r['source']}]\n{r['text']}" for r in picked)
        self.messages[-1]["content"] += (
            "\n\n---\nИз базы знаний компании (найдено автоматически по вопросу; опирайся на это, если относится "
            "к делу, и назови источник; если не относится — не упоминай):\n" + note)

    async def _run_turn(self) -> None:
        try:
            await self._auto_kb()
            for _ in range(config.MAX_AGENT_STEPS):
                final = None
                async for ev in self.client.stream_chat(self.http, self.messages, openai_tools(self.tool_groups)):
                    if ev["type"] == "text":
                        await self.emit({"type": "text_delta", "text": ev["text"]})
                    else:
                        final = ev
                assistant = {"role": "assistant", "content": final["content"] or None}
                if final["tool_calls"]:
                    assistant["tool_calls"] = final["tool_calls"]
                self.messages.append(assistant)
                if not final["tool_calls"]:
                    if self.errors_in_row:
                        # Ответ после неудачного обращения к базе: модель склонна назвать число, которого не
                        # получала. Текст уже показан — честно помечаем, что данными он не подтверждён.
                        note = ("\n\n> ⚠ Последнее обращение к базе завершилось ошибкой — цифры в этом ответе "
                                "не подтверждены данными. Переспросите или уточните вопрос.")
                        self.messages[-1]["content"] = (self.messages[-1]["content"] or "") + note
                        await self.emit({"type": "text_delta", "text": note})
                    await self.emit({"type": "turn_done"})
                    return
                await self.emit({"type": "text_end"})
                question = None
                for call in final["tool_calls"]:
                    if call["function"]["name"] == "ask_user":
                        question = question or _parse_args(call)
                        result = json.dumps({"ok": True, "result": "Вопрос показан пользователю, его ответ придёт следующим сообщением."},
                                            ensure_ascii=False)
                    else:
                        result = await self._call_tool(call)
                    self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                if question is not None:
                    # Уточняющий вопрос завершает ход: показываем его как ответ агента (с кнопками вариантов)
                    # и ждём сообщения пользователя.
                    text = str(question.get("question") or "Уточните, пожалуйста.").strip()
                    options = self._grounded_options(question.get("options"))
                    self.messages.append({"role": "assistant", "content": text + (
                        "\nВарианты: " + "; ".join(options) if options else "")})
                    await self.emit({"type": "text_delta", "text": text})
                    if options:
                        await self.emit({"type": "ask", "options": options})
                    await self.emit({"type": "turn_done"})
                    return
                if self.errors_in_row >= MAX_ERRORS_IN_ROW:
                    # Модели после серии ошибок слово не даём: локальные модели в этот момент
                    # склонны «отвечать» выдуманными цифрами. Сообщаем пользователю факты сами.
                    text = (f"Не удалось получить данные: {self.errors_in_row} ошибки подряд при обращении к базе.\n\n"
                            f"Последняя ошибка: `{self.last_error}`\n\n"
                            "Попробуйте переформулировать вопрос или уточнить, где искать данные.")
                    self.messages.append({"role": "assistant", "content": text})
                    self.errors_in_row = 0
                    await self.emit({"type": "text_delta", "text": text})
                    await self.emit({"type": "turn_done"})
                    return
            await self.emit({"type": "error", "message": f"Достигнут лимит шагов ({config.MAX_AGENT_STEPS})."})
            await self.emit({"type": "turn_done"})
        except asyncio.CancelledError:
            for fut in self.pending.values():
                fut.cancel()
            self.pending.clear()
            self._drop_dangling_tool_calls()
            await self.emit({"type": "turn_done", "interrupted": True})
        except Exception as exc:  # отдаём ошибку в интерфейс, а не роняем соединение
            log.exception("turn failed")
            self._drop_dangling_tool_calls()
            await self.emit({"type": "error", "message": str(exc)})
            await self.emit({"type": "turn_done"})

    def _drop_dangling_tool_calls(self) -> None:
        """После прерывания история не должна заканчиваться tool_call без ответа — иначе LLM API ругается."""
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for m in self.messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                missing = [c for c in m["tool_calls"] if c["id"] not in answered]
                for c in missing:
                    self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": '{"error": "прервано пользователем"}'})

    async def _call_tool(self, call: dict) -> str:
        name = call["function"]["name"]
        tool = BY_NAME.get(name)
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"аргументы не JSON: {exc}"}, ensure_ascii=False)
        if tool is None:
            return json.dumps({"error": f"нет инструмента {name}"}, ensure_ascii=False)
        if name == "query_run" and isinstance(args.get("text"), str):
            args["text"] = _fix_query(args["text"])

        event = {
            "type": "tool_call",
            "id": call["id"],
            "name": name,
            "args": args,
            "title": _safe_title(tool, args),
            "kind": tool.kind,
            "needs_approval": tool.needs_approval,
            "executor": tool.executor,
        }
        guard = self._unconfirmed_numbers(name, args)
        if guard:
            # Данных от пользователя не хватает — в 1С не идём. В чате это не «неудачное действие»,
            # а проверка: видно, что агент сверил данные и сейчас уточнит недостающее.
            await self.emit({**event, "executor": "server", "needs_approval": False, "dry_run": True,
                             "kind": "read", "title": "Проверяю, хватает ли данных для документа"})
            await self.emit({"type": "tool_result", "id": call["id"], "ok": True,
                             "summary": "нужно уточнить: " + "; ".join(self._missing_data(name, args))[:200]})
            payload = {"ok": False, "error": guard}
        elif name in self.dry_run_tools:
            # Тестовый прогон: показываем попытку, но в 1С ничего не делаем — как будто пользователь отказал.
            # executor="server": страница пересылает в 1С только клиентские вызовы — этот лишь рисует.
            await self.emit({**event, "executor": "server", "needs_approval": False, "dry_run": True})
            payload = {"ok": False, "error": "Пользователь отклонил действие (тестовый прогон: ничего не выполнялось)"}
            await self.emit({"type": "tool_result", "id": call["id"], **payload})
        elif tool.executor == "server":
            await self.emit(event)
            payload = await asyncio.to_thread(self._run_server_tool, name, args)  # поиск по коду — секунды
            await self.emit({"type": "tool_result", "id": call["id"], **payload})
        else:
            payload = await self._run_client_tool(call["id"], event)
        return self._tool_result_text(name, args, payload)

    def _date_hint(self, text: str) -> str:
        """Несколько документов с одним номером, а пользователь назвал дату — подсказываем, какой подходит."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("date")]
        user = next((m["content"] for m in reversed(self.messages) if m.get("role") == "user"), "")
        dates = {f"{y}-{mo}-{d}" for d, mo, y in _DATE.findall(user if isinstance(user, str) else "")}
        if len(items) < 2 or not dates:
            return text
        match = [i for i in items if str(i["date"])[:10] in dates]
        if len(match) == 1:
            data["hint"] = (f"Пользователь назвал дату — подходит один документ: {match[0].get('repr')}. "
                            "Бери его, не переспрашивай.")
        return json.dumps(data, ensure_ascii=False)

    def _grounded_options(self, options) -> list[str]:
        """Варианты-кнопки к вопросу — только из найденного в базе или из слов пользователя. Модель любит
        «для примера» придумать «Кабель ВВГ, 100 м, 150 руб.»: нажатие такой кнопки выдало бы выдумку
        за слова пользователя, и цена прошла бы проверку данных. Служебные «Другое», «Отмена» — можно."""
        if not isinstance(options, list):
            return []
        seen = "\n".join(m["content"] for m in self.messages
                         if m.get("role") in ("user", "tool") and isinstance(m.get("content"), str))
        # Основы по 4 буквы: в вопросе имена склоняются («Марию Олеговну» при «Мария Олеговна» в базе).
        seen_stems, seen_numbers = _stems(seen, 4), _numbers(seen)
        meta = {m[:4] for m in META_OPTION_STEMS}
        generic = {g[:4] for g in GENERIC_STEMS}
        kept = []
        for option in options:
            text = str(option).strip()
            if not text:
                continue
            words = _stems(text, 4) - generic - meta
            numbers = {n for n in _numbers(text) if n not in NUMBER_WORDS.values() or str(int(n)) in text}
            grounded = not words or len(words & seen_stems) >= 0.8 * len(words)
            if grounded and all(any(abs(n - k) < 0.005 for k in seen_numbers) for n in numbers):
                kept.append(text)
            else:
                log.info("вариант ответа отброшен (нет в найденном): %s", text[:120])
        return kept[:6]

    def _confirmed_text(self) -> str:
        """Что назвал пользователь: его сообщения и реплики агента, на которые он потом ответил
        (агент предложил — пользователь подтвердил или поправил). Вставки базы знаний не в счёт."""
        parts = []
        for i, m in enumerate(self.messages):
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            answered = any(later.get("role") == "user" for later in self.messages[i + 1:])
            if m.get("role") == "user" or (m.get("role") == "assistant" and answered):
                parts.append(text.split("\n---\nИз базы знаний")[0])
        return "\n".join(parts)

    def _missing_data(self, name: str, args: dict) -> list[str]:
        """Чего из данных документа пользователь не называл: покупатель, позиции, количества, цены."""
        if name not in DATA_GUARD_TOOLS:
            return []
        said = self._confirmed_text()
        numbers, stems = _numbers(said), _stems(said)
        missing = []

        def named(value) -> bool:
            words = _stems(value.get("repr", "") if isinstance(value, dict) else str(value or "")) - GENERIC_STEMS
            return not words or bool(words & stems)

        if name in GOODS_TOOLS:
            who = args.get("counterparty")
            if not who or not named(who):
                missing.append("контрагент" + (f" («{_label(who)}» пользователь не называл)" if who else ""))
            else:
                rivals = self._ambiguous_counterparty(who, said)
                if rivals:
                    missing.append(f"какой именно контрагент: под названное подходят {len(rivals) + 1} — «{_label(who)}», «"
                                   + "», «".join(rivals) + "»; спроси, дав их в options")
            items = args.get("items") if isinstance(args.get("items"), list) else []
            if not items:
                missing.append("что именно в документе (товары или услуги)")
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not named(item.get("item")):
                    missing.append(f"позиция «{_label(item.get('item'))}» — пользователь её не называл")
                for field, title in (("quantity", "количество"), ("price", "цена")):
                    value = item.get(field)
                    if not isinstance(value, (int, float)):
                        missing.append(f"{title} для «{_label(item.get('item'))}»")
                    elif not any(abs(value - k) < 0.005 for k in numbers):
                        missing.append(f"{title} {value:g} — пользователь её не называл")
        else:  # object_create: имя элемента и числа из полей
            fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
            for key in ("Наименование", "НаименованиеПолное"):
                if fields.get(key) and not named(fields[key]):
                    missing.append(f"{key} «{fields[key]}» — пользователь этого не называл")
            for key, value in fields.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and not any(abs(value - k) < 0.005 for k in numbers):
                    missing.append(f"{key} = {value:g} — пользователь этого не называл")
        return missing

    def _ambiguous_counterparty(self, who, said: str) -> list[str]:
        """Если поиск в этом диалоге нашёл несколько контрагентов, среди которых выбранный, а пользователь
        не назвал ничего, что отличает выбранного от остальных (имя, название, ИНН), — остальные кандидаты.
        Модель иначе выбирает «первого Сидоренко» сама."""
        chosen_uuid = who.get("uuid") if isinstance(who, dict) else None
        chosen_repr = _label(who).strip().lower()
        said_stems, said_low = _stems(said, 4), said.lower()
        for m in reversed(self.messages):
            if m.get("role") != "tool":
                continue
            refs = _refs_in(m.get("content") if isinstance(m.get("content"), str) else "", "Справочник.Контрагенты")
            chosen = [r for r in refs if r["uuid"] == chosen_uuid or str(r.get("repr", "")).strip().lower() == chosen_repr]
            if len(refs) < 2 or not chosen:
                continue
            others = [r for r in refs if r is not chosen[0]]
            other_stems = set().union(*(_stems(r.get("repr", ""), 4) for r in others))
            own = _stems(chosen[0].get("repr", ""), 4) - other_stems - {g[:4] for g in GENERIC_STEMS}
            if own & said_stems or (chosen[0].get("inn") and str(chosen[0]["inn"]) in said_low):
                return []
            return [str(r.get("repr", "?")) for r in others][:5]
        return []

    def _unconfirmed_numbers(self, name: str, args: dict) -> str | None:
        """Текст отказа для модели, если данных документа не хватает (имя оставлено для совместимости)."""
        missing = self._missing_data(name, args)
        if not missing:
            return None
        return ("Не создаю: не хватает данных от пользователя — " + "; ".join(missing) + ". Ничего не подставляй "
                "сам: вызови ask_user и спроси именно это (найденные в базе варианты можно дать в options).")

    def _run_server_tool(self, name: str, args: dict) -> dict:
        try:
            if name == "file_read":
                part = args.get("part") or 1
                result = files.read_part(self.session_id, args.get("file_id", ""), int(part))
                summary = f"{result['file']}: {result['title']}"
            elif name == "file_search":
                result = files.search(self.session_id, args.get("query", ""), args.get("file_id") or None)
                summary = f"найдено строк: {result['total']}"
            elif name == "code_read":
                result = sources.read(self.context.get("config_name", ""), args.get("module", ""),
                                      _int(args.get("line")), args.get("procedure") or None, _int(args.get("start")))
                summary = f"{result['module']} [{result['source']}]"
            elif name == "code_search":
                result = sources.search(self.context.get("config_name", ""), args.get("text", ""),
                                        args.get("scope") or None, _int(args.get("limit")) or 40)
                summary = f"совпадений: {result['total']} в {result['modules']} модулях"
            elif name == "kb_search":
                result = kb.search(args.get("query", ""), args.get("kind"))
                summary = f"найдено: {result['total']}"
            else:
                raise files.FileError(f"нет серверного инструмента {name}")
            return {"ok": True, "result": json.dumps(result, ensure_ascii=False), "summary": summary}
        except (files.FileError, sources.SourceError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _run_client_tool(self, call_id: str, event: dict) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self.pending[call_id] = fut
        await self.emit(event)
        # Действие ждёт решения пользователя в окне 1С — долго; чтение отвечает за секунды — минута.
        timeout = config.TOOL_TIMEOUT_SEC if event.get("needs_approval") else config.READ_TOOL_TIMEOUT_SEC
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            if event.get("needs_approval"):
                return {"ok": False, "error": "Пользователь не ответил в окне 1С за отведённое время — действие не выполнено"}
            return {"ok": False, "error": f"1С не ответила за {timeout} с. Похоже, вкладка с базой зависла или "
                                          "открыта старая сессия: обновите окно консультанта (или перезапустите вкладку)."}
        finally:
            self.pending.pop(call_id, None)

    def _tool_result_text(self, name: str, args: dict, payload: dict) -> str:
        audit.record(self.context, name, args, payload)
        if payload.get("ok"):
            self.errors_in_row = 0
            text = payload.get("result")
            text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            text = _mark_truncated(name, _explain_totals(name, text))
            if name == "object_find":
                text = self._date_hint(text)
        else:
            error = payload.get("error") or "ошибка без описания"
            # Отказ пользователя в окне 1С и отказ защиты от выдуманных чисел — не сбой обращения к базе.
            if not error.startswith(("Пользователь отклонил", "Не создаю:")):
                self.errors_in_row += 1
                self.last_error = error.replace("\n", " ")[:300]
            hint = next((h for marker, h in QUERY_ERROR_HINTS if marker in error), None)
            text = json.dumps({"error": error, **({"hint": hint} if hint else {})}, ensure_ascii=False)
        if len(text) > config.TOOL_RESULT_LIMIT:
            text = text[: config.TOOL_RESULT_LIMIT] + f"\n…[обрезано, всего {len(text)} символов — сузь запрос]"
        return text
