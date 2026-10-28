"""Реестр инструментов агента.

Один реестр на всех потребителей: агент в чате, позже MCP-сервер.
executor="client" — инструмент выполняет расширение 1С в сеансе пользователя
(с его правами); сервер только пересылает вызов и ждёт результат.
needs_approval — подсказка интерфейсу; окончательное решение о подтверждении
принимает расширение (белый список и нативный диалог на стороне 1С).
"""
import json
from dataclasses import dataclass, field
from typing import Callable

REF_SCHEMA = {
    "type": "object",
    "description": 'Ссылка на объект в формате {"_ref": "Документ.РеализацияТоваровУслуг", "uuid": "..."} — как её вернули другие инструменты',
    "properties": {"_ref": {"type": "string"}, "uuid": {"type": "string"}},
    "required": ["_ref", "uuid"],
}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    title: Callable[[dict], str]
    executor: str = "client"
    needs_approval: bool = False
    kind: str = "read"  # read | action
    long: bool = False  # длительное действие (минуты и дольше) — ждём его по config.LONG_TOOL_TIMEOUT_SEC

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def json_short(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:60] + "…"


def shown(value, default: str = "") -> str:
    """Значение аргумента для заголовка шага: ссылка → представление, строка как есть, остальное — коротко.
    Модель присылает аргументы любой формы, заголовок от этого не ломается."""
    if isinstance(value, dict):
        return str(value.get("repr") or value.get("_ref") or default)
    if isinstance(value, (list, tuple)):
        return default
    return str(value) if value not in (None, "") else default


def _ref_title(args: dict) -> str:
    return shown(args.get("ref"), "объект")


def _count(value) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


TOOLS: list[Tool] = [
    Tool(
        name="metadata_search",
        description=(
            "Найти объекты метаданных конфигурации (справочники, документы, регистры, перечисления, отчёты, "
            "обработки, планы счетов) по части имени или синонима. Используй первым шагом, чтобы узнать точное "
            "имя объекта."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Часть имени или синонима, например «реализация» или «НДС»"},
                "kind": {
                    "type": "string",
                    "description": "Необязательно: вид объектов — Справочник, Документ, РегистрНакопления, "
                    "РегистрСведений, РегистрБухгалтерии, Перечисление, Отчет, Обработка, ПланСчетов, ПланВидовХарактеристик",
                },
            },
            "required": ["query"],
        },
        title=lambda a: f"Ищу в метаданных: «{a.get('query', '')}»",
    ),
    Tool(
        name="metadata_describe",
        description=(
            "Описать структуру объекта метаданных по полному имени: реквизиты с типами, табличные части, "
            "для документов — регистры движений, для регистров — измерения, ресурсы, реквизиты. "
            "Обязательно вызывай перед написанием запроса, чтобы не выдумывать имена полей."
        ),
        parameters={
            "type": "object",
            "properties": {"full_name": {"type": "string", "description": "Например «Документ.РеализацияТоваровУслуг»"}},
            "required": ["full_name"],
        },
        title=lambda a: f"Смотрю структуру {a.get('full_name', '')}",
    ),
    Tool(
        name="object_find",
        description=(
            "Найти объекты (документы, элементы справочников) по номеру, коду или наименованию. "
            "Документы конкретного контрагента (организации, склада) ищи отбором filters — по тексту номера "
            "их не найти: {\"full_name\": \"Документ.РеализацияТоваровУслуг\", \"text\": \"\", "
            "\"filters\": {\"Контрагент\": \"Альфа-Инжиниринг\"}}. "
            "Возвращает ссылки, которые можно передать в object_get, document_movements, ui_open."
        ),
        parameters={
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "Например «Документ.РеализацияТоваровУслуг» или «Справочник.Контрагенты»"},
                "text": {"type": "string", "description": "Номер, код или часть наименования; пустая строка — любые (с filters)"},
                "filters": {"type": "object", "description": "Отбор по реквизитам шапки: {Реквизит: ссылка или наименование}, "
                                                             "например {\"Контрагент\": \"Альфа-Инжиниринг\"}"},
                "limit": {"type": "integer", "description": "Сколько вернуть, по умолчанию 20"},
            },
            "required": ["full_name", "text"],
        },
        title=lambda a: f"Ищу {a.get('full_name', '')}: «{a.get('text', '')}»"
                        + (" с отбором " + ", ".join(f"{k} = {shown(v)}" for k, v in a["filters"].items())
                           if isinstance(a.get("filters"), dict) and a["filters"] else ""),
    ),
    Tool(
        name="object_get",
        description="Прочитать данные объекта по ссылке: реквизиты шапки и строки табличных частей.",
        parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
        title=lambda a: f"Открываю {_ref_title(a)}",
    ),
    Tool(
        name="document_movements",
        description="Получить движения документа по всем регистрам (что документ записал при проведении).",
        parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
        title=lambda a: f"Смотрю движения {_ref_title(a)}",
    ),
    Tool(
        name="query_run",
        description=(
            "Выполнить запрос на языке запросов 1С (только чтение). Пиши ВЫБРАТЬ ПЕРВЫЕ N, используй "
            "ПРЕДСТАВЛЕНИЕ() для ссылок, виртуальные таблицы (.Остатки, .Обороты, .СрезПоследних) с параметрами. "
            "Параметры передавай в params: даты строкой ISO «2026-09-30T23:59:59», ссылки объектом {_ref, uuid}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст запроса"},
                "params": {"type": "object", "description": "Параметры запроса: имя → значение"},
                "limit": {"type": "integer", "description": "Максимум строк, по умолчанию 50"},
            },
            "required": ["text"],
        },
        title=lambda a: "Выполняю запрос",
    ),
    Tool(
        name="posting_check",
        description=(
            "Проверить, проведётся ли документ, НИЧЕГО не меняя: 1С проводит его в транзакции и откатывает. "
            "Возвращает сообщения, которые увидит пользователь (почему не проводится), а при успехе — будущие "
            "движения. Главный инструмент для вопроса «почему не проводится документ»."
        ),
        parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
        title=lambda a: f"Пробно провожу {_ref_title(a)} (с откатом)",
    ),
    Tool(
        name="eventlog_search",
        description=(
            "Поиск в журнале регистрации 1С: ошибки и предупреждения с текстами. По умолчанию — ошибки за последние "
            "сутки. Используй, когда пользователь пишет про ошибку, сбой, «не работает», или прислал скриншот ошибки. "
            "Нужны права администратора."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Часть текста ошибки или события"},
                "level": {"type": "string", "description": "error (по умолчанию), warning — ошибки и предупреждения, all — всё"},
                "user": {"type": "string", "description": "Имя пользователя ИБ или «me» — текущий"},
                "ref": {**REF_SCHEMA, "description": "Только события по этому объекту"},
                "from": {"type": "string", "description": "Начало, «2026-10-26» или «2026-10-26T09:00:00»"},
                "to": {"type": "string", "description": "Конец периода"},
                "limit": {"type": "integer"},
            },
        },
        title=lambda a: "Смотрю журнал регистрации" + (f": «{a.get('text')}»" if a.get("text") else ""),
    ),
    Tool(
        name="register_balance",
        description=(
            "Остатки регистра на дату БЕЗ написания запроса. Для бухучёта: register «РегистрБухгалтерии.Хозрасчетный», "
            "filters {\"Счет\": \"51\"} (код счёта, учитываются субсчета), group_by [\"Счет\"] или [\"Субконто1\"] "
            "(контрагенты, номенклатура…). Для регистров накопления — их измерения. Возвращает строки и общий итог "
            "(totals) — итоги бери из totals, не складывай строки сам."
        ),
        parameters={
            "type": "object",
            "properties": {
                "register": {"type": "string", "description": "Полное имя регистра"},
                "date": {"type": "string", "description": "На конец какого дня, «2026-09-30»; по умолчанию сегодня"},
                "filters": {"type": "object", "description": "Отборы по измерениям: {Измерение: значение или наименование}"},
                "group_by": {"type": "array", "items": {"type": "string"}, "description": "Измерения для разбивки"},
                "limit": {"type": "integer"},
            },
            "required": ["register"],
        },
        title=lambda a: f"Остатки {a.get('register', '')}" + (f" {json_short(a.get('filters'))}" if a.get("filters") else ""),
    ),
    Tool(
        name="register_turnover",
        description=(
            "Обороты регистра за период БЕЗ написания запроса (выручка, поступления, расход). Параметры как у "
            "register_balance, но вместо date — from и to. Для бухучёта даёт ОборотДт и ОборотКт; итоги — в totals. "
            "Числа проводок и списка документов не даёт — для этого account_report card."
        ),
        parameters={
            "type": "object",
            "properties": {
                "register": {"type": "string"},
                "from": {"type": "string", "description": "Начало периода, «2026-07-01»"},
                "to": {"type": "string", "description": "Конец периода, «2026-09-30»"},
                "filters": {"type": "object"},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            "required": ["register", "from", "to"],
        },
        title=lambda a: f"Обороты {a.get('register', '')} {a.get('from', '')}…{a.get('to', '')}"
                        + (f" {json_short(a.get('filters'))}" if a.get("filters") else ""),
    ),
    Tool(
        name="open_list",
        description=(
            "Открыть пользователю список документов или справочника с отбором по реквизитам на равенство "
            "(например, все счета покупателю по контрагенту). Значения отбора можно назвать наименованием."
        ),
        parameters={
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "«Документ.СчетНаОплатуПокупателю», «Справочник.Контрагенты»"},
                "filters": {"type": "object", "description": "{Реквизит: значение}"},
            },
            "required": ["full_name"],
        },
        title=lambda a: f"Открываю список {a.get('full_name', '')}" + (f" {json_short(a.get('filters'))}" if a.get("filters") else ""),
    ),
    Tool(
        name="ui_open",
        description="Открыть пользователю форму объекта в 1С (документ, элемент справочника), чтобы он увидел его сам.",
        parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
        title=lambda a: f"Открыть пользователю {_ref_title(a)}",
        needs_approval=True,
        kind="action",
    ),
]

def _goods_title(verb: str):
    def title(a: dict) -> str:
        return f"{verb}: {shown(a.get('counterparty'), 'контрагент')}, позиций: {_count(a.get('items'))}"
    return title


TOOLS.append(Tool(
    name="invoice_create",
    description=(
        "Создать в 1С черновик счёта на оплату покупателю (без проведения). Организацию, договор, ставки и суммы "
        "НДС заполнит сама 1С. Перед вызовом найди покупателя (object_find по Справочник.Контрагенты) и каждую "
        "позицию (object_find по Справочник.Номенклатура); если найдено несколько подходящих, или пользователь не "
        "назвал товары, количество или цену — сначала ask_user, ничего не подставляй. После создания пользователь сам выберет: посмотреть или провести."
    ),
    parameters={
        "type": "object",
        "properties": {
            "counterparty": REF_SCHEMA,
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": REF_SCHEMA,
                        "quantity": {"type": "number"},
                        "price": {"type": "number", "description": "Цена за единицу, руб."},
                    },
                    "required": ["item", "quantity", "price"],
                },
            },
            "organization": {**REF_SCHEMA, "description": "Организация-продавец. Нужна, только если в базе их несколько"},
            "comment": {"type": "string", "description": "Необязательный комментарий к счёту"},
        },
        "required": ["counterparty", "items"],
    },
    title=_goods_title("Создаю черновик счёта"),
    needs_approval=True,
    kind="action",
))

NAME_OR_REF = {"description": "Наименование строкой («ООО Ромашка», «Кабель ВВГ») или ссылка {_ref, uuid}"}
GOODS_ITEMS = {
    "type": "array",
    "description": "Позиции: товары и услуги вперемешку — 1С сама разнесёт их по табличным частям",
    "items": {
        "type": "object",
        "properties": {
            "item": NAME_OR_REF,
            "quantity": {"type": "number"},
            "price": {"type": "number", "description": "Цена за единицу, руб."},
        },
        "required": ["item", "quantity", "price"],
    },
}


TOOLS.append(Tool(
    name="sale_create",
    description=(
        "Создать черновик реализации (продажи) товаров и услуг покупателю — Документ.РеализацияТоваровУслуг. "
        "Счета учёта, ставки и суммы НДС, договор заполнит 1С. Покупателя и номенклатуру можно назвать текстом. "
        "Цену, количество и номенклатуру не выдумывай — сначала ask_user. Товары и услуги в одной реализации БП не совмещает — их "
        "оформляют двумя документами. После создания пользователь решит в окне 1С: посмотреть или провести."
    ),
    parameters={
        "type": "object",
        "properties": {
            "counterparty": {**NAME_OR_REF, "description": "Покупатель: наименование или ссылка"},
            "items": GOODS_ITEMS,
            "warehouse": {**NAME_OR_REF, "description": "Склад, если назван"},
            "contract": {**NAME_OR_REF, "description": "Договор, если у покупателя их несколько"},
            "organization": {**NAME_OR_REF, "description": "Организация, если в базе их несколько"},
            "comment": {"type": "string"},
        },
        "required": ["counterparty", "items"],
    },
    title=_goods_title("Создаю реализацию"),
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="purchase_create",
    description=(
        "Создать черновик поступления товаров и услуг от поставщика — Документ.ПоступлениеТоваровУслуг. "
        "Счета учёта, ставки и суммы НДС, договор заполнит 1С. Поставщика и номенклатуру можно назвать текстом. "
        "Если известны номер и дата документа поставщика (накладной, УПД) — передай их."
    ),
    parameters={
        "type": "object",
        "properties": {
            "counterparty": {**NAME_OR_REF, "description": "Поставщик: наименование или ссылка"},
            "items": GOODS_ITEMS,
            "incoming_number": {"type": "string", "description": "Номер документа поставщика"},
            "incoming_date": {"type": "string", "description": "Дата документа поставщика, 2026-10-26"},
            "warehouse": {**NAME_OR_REF, "description": "Склад, если назван"},
            "contract": {**NAME_OR_REF, "description": "Договор, если у поставщика их несколько"},
            "organization": {**NAME_OR_REF, "description": "Организация, если в базе их несколько"},
            "comment": {"type": "string"},
        },
        "required": ["counterparty", "items"],
    },
    title=_goods_title("Создаю поступление"),
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="create_based_on",
    description=(
        "Создать документ НА ОСНОВАНИИ другого — типовым механизмом конфигурации, как команда «Создать на "
        "основании»: счёт-фактура на реализацию, реализация по счёту, оплата по счёту и т.п. Заполнение делает 1С, "
        "это надёжнее ручного. base — ссылка на документ-основание (найди через object_find), target — вид "
        "создаваемого документа. Если такой ввод невозможен, 1С вернёт список допустимых документов."
    ),
    parameters={
        "type": "object",
        "properties": {
            "base": REF_SCHEMA,
            "target": {"type": "string", "description": "«Документ.СчетФактураВыданный», «Документ.РеализацияТоваровУслуг»…"},
        },
        "required": ["base", "target"],
    },
    title=lambda a: f"Создаю {shown(a.get('target'), 'документ')} на основании {shown(a.get('base'), 'документа')}",
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="document_post",
    description="Провести существующий документ. Пользователь подтвердит в окне 1С. Перед этим полезно posting_check.",
    parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
    title=lambda a: f"Провести {_ref_title(a)}",
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="document_unpost",
    description="Отменить проведение документа. Пользователь подтвердит в окне 1С.",
    parameters={"type": "object", "properties": {"ref": REF_SCHEMA}, "required": ["ref"]},
    title=lambda a: f"Отменить проведение {_ref_title(a)}",
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="object_update",
    description=(
        "Изменить реквизиты шапки существующего документа или элемента справочника. Пользователь увидит в окне 1С "
        "«было → станет» и подтвердит; проведённый документ будет перепроведён (если не проведётся — ничего не "
        "запишется). Значения-ссылки можно назвать наименованием. Проведение и пометку удаления так не меняют."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": REF_SCHEMA,
            "fields": {"type": "object", "description": "{Реквизит: новое значение}, имена — из metadata_describe"},
        },
        "required": ["ref", "fields"],
    },
    title=lambda a: f"Изменяю {_ref_title(a)}: " + (", ".join(a["fields"]) if isinstance(a.get("fields"), dict) else "реквизиты"),
    needs_approval=True,
    kind="action",
))

TOOLS.append(Tool(
    name="object_create",
    description=(
        "Создать в 1С документ (черновиком, без проведения) или элемент справочника. Сначала вызови "
        "metadata_describe — реквизиты с пометкой [обязательный] заполни. Значения: ссылку на справочник "
        "можно передать просто наименованием или кодом строкой («ООО Ромашка», «шт») — 1С найдёт сама, "
        "при неоднозначности вернёт варианты; перечисление — именем значения; дату — «2026-10-26»; "
        "число — числом. Для счёта покупателю — invoice_create, реализации — sale_create, поступления товаров "
        "и услуг — purchase_create (там учёт и НДС заполнит 1С). После создания "
        "пользователь сам выберет в окне 1С, что делать дальше."
    ),
    parameters={
        "type": "object",
        "properties": {
            "full_name": {"type": "string", "description": "«Документ.ПоступлениеНаРасчетныйСчет» или «Справочник.Номенклатура»"},
            "fields": {"type": "object", "description": "Реквизиты шапки: {ИмяРеквизита: значение}"},
            "tables": {
                "type": "object",
                "description": "Табличные части: {ИмяТабличнойЧасти: [{Колонка: значение}, …]}",
            },
        },
        "required": ["full_name", "fields"],
    },
    title=lambda a: f"Создаю {shown(a.get('full_name'), 'объект')}"
                    + (f": {shown(a['fields'].get('Наименование'))}"
                       if isinstance(a.get("fields"), dict) and a["fields"].get("Наименование") else ""),
    needs_approval=True,
    kind="action",
))

TOOLS += [
    Tool(
        name="file_read",
        description=(
            "Прочитать часть прикреплённого пользователем файла (страницу PDF, кусок листа Excel, кусок текста). "
            "Номера частей — с 1; сколько их, указано при прикреплении файла."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Идентификатор файла, например f1a2b3c4d"},
                "part": {"type": "integer", "description": "Номер части, по умолчанию 1"},
            },
            "required": ["file_id"],
        },
        title=lambda a: f"Читаю файл {a.get('file_id', '')}, часть {a.get('part', 1)}",
        executor="server",
    ),
    Tool(
        name="file_search",
        description=(
            "Найти в прикреплённых файлах строки, содержащие все слова запроса (номер документа, ИНН, "
            "наименование, сумму). Без file_id — ищет во всех файлах диалога."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова для поиска"},
                "file_id": {"type": "string", "description": "Необязательно: искать только в этом файле"},
            },
            "required": ["query"],
        },
        title=lambda a: f"Ищу в файлах: «{a.get('query', '')}»",
        executor="server",
    ),
]

TOOLS += [
    Tool(
        name="regulated_report",
        description=(
            "Регламентированные отчёты, сохранённые в базе: декларации (НДС, УСН, 6-НДФЛ, РСВ…), бухотчётность. "
            "kind=list — список (report_type: «НДС», «Бух», «6_НДФЛ»…; from/to — период окончания). kind=read — "
            "показатели отчёта ref: раздел, строка, графа, название строки из формы и значение; section сужает "
            "(«Раздел1», «Раздел3», «ОФР», «Баланс»). Числа бери отсюда, не считай сам."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["list", "read"]},
                "report_type": {"type": "string", "description": "Часть имени вида отчёта: НДС, Бух, УСН, 6_НДФЛ"},
                "from": {"type": "string"}, "to": {"type": "string"},
                "ref": {**REF_SCHEMA, "description": "Отчёт из kind=list"},
                "section": {"type": "string"},
            },
            "required": ["kind"],
        },
        title=lambda a: ("Читаю отчёт " + shown(a.get("ref"), "") + (f", {shown(a.get('section'))}" if a.get("section") else ""))
                        if a.get("kind") == "read" else f"Ищу регламентированные отчёты: {shown(a.get('report_type'), 'все')}",
    ),
    Tool(
        name="vat_book",
        description=(
            "Книга продаж (book=sales) или покупок (book=purchases) за период: итог НДС и суммы без НДС и разбивка "
            "group_by: rate (ставка), counterparty, invoice (счета-фактуры), event (реализация / аванс / …). "
            "counterparty — отбор по контрагенту. Доп. листы — additional_sheets=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book": {"type": "string", "enum": ["sales", "purchases"]},
                "from": {"type": "string"}, "to": {"type": "string"},
                "group_by": {"type": "string", "enum": ["rate", "counterparty", "invoice", "event"]},
                "counterparty": NAME_OR_REF,
                "additional_sheets": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["book", "from", "to"],
        },
        title=lambda a: ("Книга продаж" if a.get("book") != "purchases" else "Книга покупок")
                        + f" {shown(a.get('from'))}…{shown(a.get('to'))}",
    ),
    Tool(
        name="vat_reconcile",
        description=(
            "Сверка НДС за квартал (date — любая дата квартала): декларация (стр. 118, 190, 200, раздел 1 стр. 040) "
            "↔ книга продаж и книга покупок ↔ оборот Кт 68.02. Каждая проверка — суммы и разница; до 1 руб. — "
            "округление. Для «сходится ли декларация», «откуда расхождение по НДС»."
        ),
        parameters={"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]},
        title=lambda a: f"Сверяю НДС за квартал {shown(a.get('date'))}",
    ),
    Tool(
        name="ask_user",
        description=(
            "Задать пользователю уточняющий вопрос и дождаться ответа. Обязательно перед созданием документа, если "
            "пользователь не назвал контрагента, товары/услуги, количество или цену, а также когда подходит несколько "
            "вариантов (контрагентов, договоров, номенклатуры) или вопрос неоднозначен. Спроси всё недостающее одним "
            "вопросом. Не спрашивай то, что заполнит 1С сама: НДС, счета учёта, организацию, склад, единственный договор. "
            "options — до 6 готовых ответов-кнопок (например, найденные в базе варианты)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Вопрос пользователю"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "Готовые варианты ответа"},
            },
            "required": ["question"],
        },
        title=lambda a: "Уточняю у пользователя",
        executor="server",
    ),
    Tool(
        name="account_report",
        description=(
            "Бухгалтерские отчёты по счёту за период без написания запроса. kind: osv — оборотно-сальдовая ведомость "
            "(без account — по счетам первого уровня, с account — по субсчетам); analysis — счёт в разрезе субконто "
            "(контрагенты, договоры, номенклатура; group_by [\"Субконто1\"]); card — карточка счёта: проводки по дням "
            "с корреспонденцией и нарастающим сальдо. counterparty — отбор по контрагенту (наименованием или ссылкой), "
            "для акта сверки и «откуда сумма». Сальдо развёрнутое: Дт и Кт отдельно; итоги — в totals."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["osv", "analysis", "card"]},
                "account": {"type": "string", "description": "Код счёта: «62», «51», «60.01»"},
                "from": {"type": "string", "description": "Начало периода, «2026-09-01»"},
                "to": {"type": "string", "description": "Конец периода, «2026-09-30»"},
                "counterparty": NAME_OR_REF,
                "subconto": {**REF_SCHEMA, "description": "Отбор по любому объекту аналитики ссылкой {_ref, uuid}"},
                "group_by": {"type": "array", "items": {"type": "string"}, "description": "Для analysis: Субконто1..3, Счет"},
                "register": {"type": "string", "description": "По умолчанию РегистрБухгалтерии.Хозрасчетный"},
                "limit": {"type": "integer"},
            },
            "required": ["kind", "from", "to"],
        },
        title=lambda a: {"osv": "ОСВ", "analysis": "Анализ счёта", "card": "Карточка счёта"}.get(shown(a.get("kind")), "Отчёт по счёту")
                        + (f" {shown(a.get('account'))}" if a.get("account") else "")
                        + f" {shown(a.get('from'))}…{shown(a.get('to'))}"
                        + (f", {shown(a.get('counterparty'))}" if shown(a.get("counterparty")) else ""),
    ),
    Tool(
        name="report_run",
        description=(
            "Сформировать отчёт конфигурации на СКД и получить его строки (как видит пользователь). report — полное "
            "имя («Отчет.ВедомостьПоТоварамНаСкладах»), найди через metadata_search kind Отчет. from/to подставятся "
            "в параметры периода; params — другие параметры {Имя: значение}; filters — отборы {Поле: значение}. "
            "Если параметр обязателен, ошибка перечислит доступные. Для ОСВ и карточки счёта есть account_report."
        ),
        parameters={
            "type": "object",
            "properties": {
                "report": {"type": "string"},
                "variant": {"type": "string", "description": "Имя или название варианта; по умолчанию первый"},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "params": {"type": "object"},
                "filters": {"type": "object"},
                "limit": {"type": "integer"},
            },
            "required": ["report"],
        },
        title=lambda a: f"Формирую отчёт {a.get('report', '')}" + (f" {a.get('from', '')}…{a.get('to', '')}" if a.get("from") else ""),
    ),
    Tool(
        name="extensions_list",
        description=(
            "Какие расширения конфигурации установлены (версия, активность, безопасный режим) и какие объекты "
            "конфигурации они дорабатывают. Нужен, когда поведение отличается от типового или ошибка в доработанном объекте."
        ),
        parameters={"type": "object", "properties": {
            "object": {"type": "string", "description": "Проверить конкретный объект: «Документ.РеализацияТоваровУслуг» "
                       "или имя модуля из ошибки — ответ, изменён ли он расширениями"},
        }},
        title=lambda a: "Смотрю расширения конфигурации" + (f": {a['object']}" if a.get("object") else ""),
    ),
    Tool(
        name="code_read",
        description=(
            "Прочитать исходный код модуля конфигурации (по выгрузке в файлы). module — имя как в тексте ошибки 1С: "
            "«Документ.РеализацияТоваровУслуг.МодульОбъекта», «ОбщийМодуль.ОбщегоНазначения», "
            "«Документ.X.Форма.ФормаДокумента.Форма»; можно передать прямо фрагмент ошибки «{…МодульОбъекта(512)}». "
            "С line — код вокруг строки и процедура, в которой она; с procedure — текст процедуры; без них — список процедур. "
            "По умолчанию — код основной конфигурации; source — имя расширения (из extensions_list), чтобы прочитать "
            "его код. Если модуль доработан расширением, в ответе also_in: что расширение перехватывает (&Вместо…)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "module": {"type": "string"},
                "line": {"type": "integer", "description": "Номер строки из текста ошибки"},
                "procedure": {"type": "string", "description": "Имя процедуры или функции"},
                "start": {"type": "integer", "description": "С какой строки продолжить длинную процедуру"},
                "source": {"type": "string", "description": "Имя расширения или «Конфигурация»"},
            },
            "required": ["module"],
        },
        title=lambda a: f"Читаю код {a.get('module', '')}" + (f", строка {a['line']}" if a.get("line") else "")
                        + (f", {a['procedure']}" if a.get("procedure") else "")
                        + (f" [{a['source']}]" if a.get("source") else ""),
        executor="client",
    ),
    Tool(
        name="code_search",
        description=(
            "Найти в коде конфигурации и расширений место по тексту: имя процедуры, реквизита, регистра, фрагмент "
            "сообщения об ошибке («Не заполнено поле…»). scope сужает поиск: «Документ.РеализацияТоваровУслуг», "
            "«ОбщийМодуль». source — только в коде этого расширения (имя из extensions_list): для вопросов «что "
            "меняет расширение». Возвращает модуль(строка): текст строки; строки расширений помечены [имя]."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Искомый текст, от 3 символов"},
                "scope": {"type": "string", "description": "Необязательно: вид объекта или объект"},
                "source": {"type": "string", "description": "Необязательно: имя расширения или «Конфигурация»"},
                "limit": {"type": "integer"},
            },
            "required": ["text"],
        },
        title=lambda a: f"Ищу в коде: «{a.get('text', '')}»" + (f" в {a['scope']}" if a.get("scope") else "")
                        + (f" [{a['source']}]" if a.get("source") else ""),
        executor="client",
    ),
    Tool(
        name="kb_search",
        description=(
            "Поиск по базе знаний компании: инструкции, регламенты, памятки и решённые обращения (симптом → причина "
            "→ решение). Используй для вопросов «как сделать», «почему так», «что делать, если» — до того, как "
            "отвечать по памяти. kind: tickets — только похожие обращения, docs — только документы."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Суть вопроса своими словами"},
                "kind": {"type": "string", "enum": ["all", "tickets", "docs"]},
            },
            "required": ["query"],
        },
        title=lambda a: f"Ищу в базе знаний: «{a.get('query', '')}»",
        executor="client",
    ),
]


# Группы инструментов: модели отдаём только нужные вопросу. Все 28 описаний — около четверти
# контекста локальной модели; лишние замедляют первый ответ и сбивают выбор.
TOOLS += [
    Tool(
        name="month_close_status",
        description=(
            "Состояние закрытия месяца по организации: регламентные операции с состояниями (Выполнено / Не выполнено / "
            "Выполнено с ошибками / Пропущено, outdated — устарела), тексты ошибок операций, число документов к "
            "перепроведению, закрыт ли месяц. period — «2026-09». Вызывай после month_close_run, чтобы показать итог."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Месяц: «2026-09»"},
                "organization": {"type": "string", "description": "Наименование организации; если в базе одна — не нужно"},
            },
            "required": ["period"],
        },
        title=lambda a: f"Состояние закрытия месяца: {shown(a.get('period'))}",
    ),
    Tool(
        name="month_close_check",
        description=(
            "Что мешает закрыть месяц: непроведённые документы периода, отрицательные остатки запасов и денег, "
            "деньги в пути (57), НДС не к вычету (19), документы к перепроведению, регламентные операции с ошибками "
            "и устаревшие. Вызывай на «можно ли закрыть», «что мешает», «проверь перед закрытием» и всегда перед month_close_run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Месяц: «2026-09»"},
                "organization": {"type": "string", "description": "Наименование организации; если в базе одна — не нужно"},
            },
            "required": ["period"],
        },
        title=lambda a: f"Проверяю готовность к закрытию: {shown(a.get('period'))}",
    ),
    Tool(
        name="month_close_run",
        description=(
            "Выполнить закрытие месяца: перепроведение документов (если требуется) и все регламентные операции по порядку — "
            "как кнопка «Выполнить закрытие месяца». Пользователь подтвердит в окне 1С. Только по явной просьбе закрыть "
            "месяц и после month_close_check. Возвращает выполненные операции и итоговое состояние с ошибками. "
            "until — kind операции из month_close_status, до которой выполнять включительно."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Месяц: «2026-09»"},
                "organization": {"type": "string", "description": "Наименование организации; если в базе одна — не нужно"},
                "until": {"type": "string", "description": "Имя вида операции (kind), до которой выполнять; пусто — все"},
            },
            "required": ["period"],
        },
        title=lambda a: f"Закрыть месяц {shown(a.get('period'))}" + (f" ({shown(a['organization'])})" if a.get("organization") else "")
                        + (f" до операции {shown(a['until'])}" if a.get("until") else ""),
        needs_approval=True,
        kind="action",
        long=True,
    ),
]

BY_NAME = {t.name: t for t in TOOLS}

GROUPS = {
    "core": ["metadata_search", "metadata_describe", "object_find", "object_get", "query_run", "kb_search", "ask_user"],
    "data": ["register_balance", "register_turnover", "account_report", "report_run", "document_movements"],
    "diagnostics": ["posting_check", "eventlog_search", "extensions_list", "code_read", "code_search",
                    "document_movements"],
    "actions": ["invoice_create", "sale_create", "purchase_create", "create_based_on", "object_create",
                "object_update", "document_post", "document_unpost", "open_list", "ui_open"],
    "files": ["file_read", "file_search"],
    "reporting": ["regulated_report", "vat_book", "vat_reconcile", "account_report"],
    "closing": ["month_close_status", "month_close_check", "month_close_run", "posting_check", "account_report"],
}


def openai_tools(groups: set[str] | None = None) -> list[dict]:
    if groups is None:
        return [t.openai_schema() for t in TOOLS]
    names = {n for g in groups | {"core"} for n in GROUPS[g]}
    return [t.openai_schema() for t in TOOLS if t.name in names]
