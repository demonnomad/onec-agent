/* Веб-интерфейс чата — «красивый терминал».
 *
 * Загружается в поле HTML формы 1С (или на /dev в обычном браузере).
 * Мост с 1С — только строки и клики в DOM, без вызова JS-функций из 1С:
 * веб-клиент 1С падает целиком на обращении к window страницы.
 *   #bridge-out (textarea)  — страница кладёт сюда JSON очередного вызова
 *                             инструмента; 1С читает .value и очищает поле;
 *   #bridge-in  (textarea)  — 1С пишет сюда "вид\nданные" и жмёт .click()
 *   #bridge-btn (button)      по кнопке; виды: hello (контекст JSON),
 *                             result (ответ инструмента JSON), reset.
 *   #bridge-ping (button)   — страница «нажимает» её, положив вызов в #bridge-out:
 *                             форма ловит клик в ПриНажатии поля HTML и забирает
 *                             вызов сразу. Таймер опроса в 1С — только запасной путь:
 *                             в скрытой вкладке браузер тормозит его до минуты.
 * Готовность страницы 1С узнаёт по document.title === "onec-agent-ready".
 * Подтверждение действий показывает сама 1С нативным диалогом, не этот интерфейс.
 */
(function () {
  "use strict";
  // Скрипт мог подключиться дважды (перезагрузка формы) — второй раз не стартуем.
  if (window.bridgePop) return;
  const script = document.currentScript;
  const origin = script ? new URL(script.src).origin : location.origin;
  const wsUrl = origin.replace(/^http/, "ws") + "/ws";

  // ---------- тема ----------
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* хранилище недоступно — не страшно */ } },
  };
  function applyTheme(t) { document.documentElement.setAttribute("data-theme", t); }
  const prefersLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  applyTheme(store.get("onec-agent-theme") || (prefersLight ? "light" : "dark"));

  // ---------- каркас ----------
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = origin + "/ui.css";
  document.head.appendChild(css);
  document.body.innerHTML =
    '<div id="app">' +
    '<div id="top"><span id="conn" title="Связь с сервером"></span>' +
    '<span class="logo">◆ <span>ИИ-консультант</span></span>' +
    '<span id="meta">подключение…</span>' +
    '<button class="icon-btn" id="gear" title="Настройки модели" hidden>⚙</button>' +
    '<button class="icon-btn" id="theme" title="Светлая / тёмная тема">◐</button>' +
    '<button class="icon-btn" id="help" title="Справка">?</button></div>' +
    '<div id="log"></div>' +
    '<div id="files"></div>' +
    '<div id="bar"><span class="prompt">›</span>' +
    '<textarea id="input" rows="1" placeholder="Спросите о базе или попросите что-то сделать…"></textarea>' +
    '<button class="btn" id="attach" title="Прикрепить файл">📎</button>' +
    '<button class="btn" id="send" title="Отправить (Enter)">↵</button>' +
    '<button class="btn" id="stop" title="Остановить (Esc)">■ Стоп</button></div>' +
    '<div id="hint"><span>Enter — отправить · Shift+Enter — новая строка · Esc — стоп</span><span id="hint-r"></span></div>' +
    '<input id="picker" type="file" multiple hidden></div>' +
    '<textarea id="bridge-out" hidden></textarea><textarea id="bridge-in" hidden></textarea>' +
    '<button id="bridge-btn" hidden></button><button id="bridge-ping" hidden></button>';
  const $ = (id) => document.getElementById(id);
  const logEl = $("log"), input = $("input"), app = $("app");

  // ---------- утилиты ----------
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  function append(node) { logEl.appendChild(node); scroll(); return node; }
  function scroll() {
    // Не дёргаем ленту, если пользователь отлистал вверх читать.
    if (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 160) logEl.scrollTop = logEl.scrollHeight;
  }
  function secs(ms) { return ms < 1000 ? ms + " мс" : (ms / 1000).toFixed(ms < 10000 ? 1 : 0).replace(".", ",") + " с"; }
  const numFmt = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  function fmtValue(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "number") return numFmt.format(v);
    if (typeof v === "boolean") return v ? "да" : "нет";
    if (typeof v === "object") return v.repr !== undefined ? String(v.repr) : JSON.stringify(v);
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(v);
    if (m) return m[3] + "." + m[2] + "." + m[1] + (m[4] + m[5] + m[6] !== "000000" ? " " + m[4] + ":" + m[5] : "");
    return String(v);
  }
  const isNumCell = (s) => /^-?[\d\s ]+([.,]\d+)?(\s?(₽|руб\.?|%))?$/.test(String(s).trim()) && /\d/.test(s);

  // ---------- подсветка языка 1С (запросы и BSL) ----------
  const KW = new Set(("выбрать из где как и или не сгруппировать по упорядочить первые различные разрешенные сумма количество " +
    "среднее максимум минимум между подобно в есть null выбор когда тогда иначе конец левое правое внутреннее полное " +
    "соединение объединить все поместить уничтожить имеющие датавремя представление значение убыв возр истина ложь " +
    "неопределено если иначеесли конецесли для каждого цикл конеццикла пока процедура функция конецпроцедуры " +
    "конецфункции возврат новый попытка исключение конецпопытки перем экспорт знач итоги спецсимвол select from where " +
    "and or not group by order as").split(" "));
  function highlight(code) {
    const re = /(\/\/[^\n]*)|("(?:[^"]|"")*"?)|(&[\wА-Яа-яЁё]+)|(\b\d+(?:[.,]\d+)?\b)|([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)|([\s\S])/g;
    let out = "", m;
    while ((m = re.exec(code))) {
      if (m[1]) out += '<span class="c">' + esc(m[1]) + "</span>";
      else if (m[2]) out += '<span class="s">' + esc(m[2]) + "</span>";
      else if (m[3]) out += '<span class="p">' + esc(m[3]) + "</span>";
      else if (m[4]) out += '<span class="nu">' + esc(m[4]) + "</span>";
      else if (m[5]) out += KW.has(m[5].toLowerCase()) ? '<span class="kw">' + esc(m[5]) + "</span>" : esc(m[5]);
      else out += esc(m[6]);
    }
    return out;
  }
  function codeBlock(code, lang) {
    const bsl = /^(bsl|1c|1с|sdbl|query)$/i.test(lang || "") || /ВЫБРАТЬ|Процедура|Функция/.test(code);
    const pre = el("pre");
    pre.innerHTML = "<code>" + (bsl ? highlight(code) : esc(code)) + "</code>";
    const b = el("button", "btn copy", "копировать");
    b.onclick = (e) => { e.stopPropagation(); copyText(code); b.textContent = "скопировано"; setTimeout(() => (b.textContent = "копировать"), 1200); };
    pre.appendChild(b);
    return pre;
  }
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).catch(() => fallback());
    else fallback();
    function fallback() {
      const ta = el("textarea"); ta.value = t; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* буфер недоступен */ }
      ta.remove();
    }
  }

  // ---------- таблицы ----------
  function table(columns, rows, total) {
    const wrap = el("div", "tbl-wrap");
    const t = el("table", "t");
    const numeric = columns.map((_, ci) => rows.length > 0 && rows.every((r) => r[ci] === "" || r[ci] === null || isNumCell(r[ci])));
    t.innerHTML = "<tr>" + columns.map((c, ci) => '<th class="' + (numeric[ci] ? "n" : "") + '">' + esc(c) + "</th>").join("") + "</tr>" +
      rows.map((r) => "<tr>" + r.map((v, ci) => '<td class="' + (numeric[ci] ? "n" : "") + '">' + esc(v === null ? "" : v) + "</td>").join("") + "</tr>").join("");
    wrap.appendChild(t);
    if (total && total > rows.length) wrap.appendChild(el("div", "more", "показано " + rows.length + " из " + total));
    return wrap;
  }

  // ---------- мини-markdown ----------
  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  }
  function md(src) {
    const root = el("div");
    const lines = src.split("\n");
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      const fence = l.match(/^```\s*(\S*)/);
      if (fence) {
        const code = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        root.appendChild(codeBlock(code.join("\n"), fence[1])); i++; continue;
      }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim().replace(/\*\*/g, ""));
        const head = cells(l); const body = []; i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(cells(lines[i++]));
        root.appendChild(table(head, body)); continue;
      }
      const h = l.match(/^(#{1,4})\s+(.*)/);
      if (h) { root.appendChild(el("h3", "", inline(h[2]))); i++; continue; }
      if (/^\s*([-*•]|\d+\.)\s+/.test(l)) {
        const ordered = /^\s*\d+\./.test(l);
        const list = el(ordered ? "ol" : "ul");
        while (i < lines.length && /^\s*([-*•]|\d+\.)\s+/.test(lines[i]))
          list.appendChild(el("li", "", inline(lines[i++].replace(/^\s*([-*•]|\d+\.)\s+/, ""))));
        root.appendChild(list); continue;
      }
      if (l.trim() === "") { i++; continue; }
      const para = [l]; i++;
      while (i < lines.length && lines[i].trim() !== "" && !/^(```|\s*\||#|\s*([-*•]|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
      root.appendChild(el("p", "", para.map(inline).join("<br>")));
    }
    return root;
  }

  // ---------- состояние ----------
  let ws = null, context = null, standalone = false, busy = false, model = "";
  // Токен сервера: из 1С — в контексте hello, на странице /dev — в адресе после # (#token=…),
  // который не уходит на сервер и не попадает в журнал доступа.
  let token = (location.hash.match(/token=([^&]+)/) || [])[1] || "", denied = false;
  const hello = () => ({ type: "hello", context: context, session: sessionId, token: token });
  let modelInfo = null;   // {label, cloud, can_edit} — источник модели этой базы
  // Идентификатор диалога: к нему сервер привязывает вложения. Случайный и длинный —
  // знание его даёт доступ к файлам диалога.
  const sessionId = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  const attachments = []; // {id?, name, state: "uploading"|"ready"|"error", error?}
  const outbox = [];      // вызовы инструментов, ожидающие 1С
  const tools = {};       // id → {el, started, name}
  let current = null;     // {el, text} — текущий ответ ассистента
  let stepsEl = null;     // текущий блок шагов
  let thinking = null;    // индикатор ожидания модели
  let turn = null;        // {started, steps}

  // Пока соединение не открыто (старт, переподключение), сообщения ждут в очереди.
  const pendingSend = [];
  // Запрос хода, на который сервер ещё ничего не ответил: если соединение оборвётся раньше
  // (рестарт сервера, сетевой сбой), после переподключения отправим его заново, а не потеряем.
  let unanswered = null;
  function send(obj) {
    if (obj.type === "user_message" || obj.type === "run_evals") unanswered = obj;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
    else pendingSend.push(obj);
  }

  function setBusy(b) {
    busy = b;
    app.classList.toggle("busy", b);
    $("conn").classList.toggle("busy", b);
    if (!b) hideThinking();
  }
  function renderMeta() {
    const parts = [];
    if (modelInfo) parts.push((modelInfo.cloud ? '<span class="cloud" title="Вопросы и данные базы уходят облачному провайдеру">☁ ' : "<b>")
      + esc(modelInfo.label) + (modelInfo.cloud ? "</span>" : "</b>"));
    else if (model) parts.push("<b>" + esc(model.split("/").pop()) + "</b>");
    if (context && context.config) parts.push(esc(context.config));
    if (context && context.user) parts.push(esc(context.user.replace(/\s+/g, " ")));
    if (standalone) parts.push("режим /dev — без 1С");
    $("meta").innerHTML = parts.join(" · ") || "подключение…";
  }

  // ---------- индикатор «модель думает» ----------
  function showThinking() {
    if (thinking || !busy) return;
    const node = el("div", "thinking", '<span class="spin">◐</span> модель думает<span class="dots"></span> <span class="t">0 с</span>');
    const started = Date.now();
    thinking = { node, timer: setInterval(() => { node.querySelector(".t").textContent = Math.round((Date.now() - started) / 1000) + " с"; }, 1000) };
    append(node);
  }
  function hideThinking() {
    if (!thinking) return;
    clearInterval(thinking.timer); thinking.node.remove(); thinking = null;
  }

  // ---------- ответ ассистента ----------
  function finishText() {
    if (current) { current.el.classList.remove("cursor"); current = null; }
  }
  function onTextDelta(t) {
    hideThinking();
    if (!current) { current = { el: append(el("div", "msg answer md cursor")), text: "" }; stepsEl = null; }
    current.text += t;
    const rendered = md(current.text);
    current.el.innerHTML = "";
    while (rendered.firstChild) current.el.appendChild(rendered.firstChild);
    scroll();
  }

  // ---------- шаги инструментов ----------
  const CREATE_TOOLS = new Set(["invoice_create", "sale_create", "purchase_create", "object_create",
    "create_based_on", "document_post", "document_unpost", "object_update"]);
  function argsPreview(name, args) {
    if (name === "query_run" || CREATE_TOOLS.has(name)) return "";
    const parts = [];
    for (const k in args) {
      if (k === "ref" || k === "limit") continue;
      const v = typeof args[k] === "object" ? JSON.stringify(args[k]) : String(args[k]);
      parts.push(v.length > 50 ? v.slice(0, 50) + "…" : v);
    }
    return parts.join(", ");
  }
  function onToolCall(ev) {
    finishText(); hideThinking();
    if (!stepsEl || stepsEl !== logEl.lastElementChild) stepsEl = append(el("div", "steps"));
    const node = el("div", "tool " + (ev.needs_approval ? "ask" : "wait") + (ev.kind === "action" ? " action" : ""));
    node.innerHTML =
      '<div class="head"><span class="st">' + (ev.needs_approval ? "⏸" : '<span class="spin">◐</span>') + "</span>" +
      '<span class="title">' + esc(ev.title) + "</span>" +
      '<span class="sum">' + esc(ev.needs_approval ? "ждёт решения в 1С…" : argsPreview(ev.name, ev.args)) + "</span>" +
      '<span class="dur"></span><span class="chev">▶</span></div><div class="body"></div>';
    const body = node.querySelector(".body");
    if (ev.name === "query_run" && ev.args.text) {
      body.appendChild(el("div", "lbl", "запрос"));
      body.appendChild(codeBlock(ev.args.text, "query"));
      if (ev.args.params) body.appendChild(el("div", "sub", esc("Параметры: " + JSON.stringify(ev.args.params))));
    } else if (Object.keys(ev.args || {}).length) {
      body.appendChild(el("div", "lbl", "аргументы"));
      body.appendChild(codeBlock(JSON.stringify(ev.args, null, 2), "json"));
    }
    node.querySelector(".head").onclick = () => node.classList.toggle("open");
    stepsEl.appendChild(node); scroll();
    tools[ev.id] = { el: node, started: Date.now(), name: ev.name };
    if (turn) turn.steps++;
    if (ev.executor === "server" || ev.dry_run) {
      // выполняет сам сервер (файлы) или это тестовый прогон — в 1С не отправляем, результат придёт tool_result
    } else if (standalone) {
      setTimeout(() => bridgeResult(JSON.stringify({ id: ev.id, ok: false, error: "нет моста 1С: чат открыт вне базы (режим /dev)" })), 300);
    } else {
      outbox.push(ev);
      flushOutbox();
    }
  }
  function resultView(raw) {
    let data = raw;
    if (typeof data === "string") { try { data = JSON.parse(data); } catch (e) { return codeBlock(String(raw), ""); } }
    if (data && Array.isArray(data.columns) && Array.isArray(data.rows))
      return table(data.columns, data.rows.slice(0, 50).map((r) => r.map(fmtValue)), data.total);
    if (data && Array.isArray(data.items) && data.items.length && typeof data.items[0] === "object") {
      const cols = Array.from(new Set(data.items.flatMap((it) => Object.keys(it)).filter((k) => !["_ref", "uuid"].includes(k))));
      return table(cols, data.items.slice(0, 50).map((it) => cols.map((c) => fmtValue(it[c]))), data.total);
    }
    if (data && Array.isArray(data.hits))
      return table(["файл", "часть", "строка"], data.hits.map((h) => [h.name, h.part, h.line]), data.total);
    if (data && typeof data.text === "string" && data.title) {
      const box = el("div");
      box.appendChild(el("div", "sub", esc(data.file + " — " + data.title)));
      box.appendChild(codeBlock(data.text.slice(0, 6000), ""));
      return box;
    }
    return codeBlock(JSON.stringify(data, null, 2).slice(0, 20000), "json");
  }
  function onToolResult(r) {
    const t = tools[r.id];
    if (!t) return;
    const node = t.el;
    node.classList.remove("wait", "ask");
    node.classList.add(r.ok ? "done" : "fail");
    node.querySelector(".st").textContent = r.ok ? "✓" : "✗";
    node.querySelector(".sum").textContent = r.ok ? (r.summary || "готово") : (r.error || "ошибка");
    node.querySelector(".dur").textContent = secs(Date.now() - t.started);
    const body = node.querySelector(".body");
    body.appendChild(el("div", "lbl", r.ok ? "результат" : "ошибка"));
    body.appendChild(r.ok ? resultView(r.result) : el("div", "err", esc(r.error || "?")));
    if (r.ok && CREATE_TOOLS.has(t.name)) docCard(r.result);
    scroll();
    if (busy) showThinking(); // дальше снова думает модель
  }
  function docCard(raw) {
    let d;
    try { d = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (e) { return; }
    if (!d || !d.repr) return;
    const card = append(el("div", "doc",
      '<span class="ic">' + (d.kind === "catalog" ? "📇" : "📄") + '</span><div><div class="nm">' + esc(d.repr) + '</div><div class="sub">' + esc(d.outcome || "") + "</div></div>" +
      (d.sum !== undefined ? '<span class="sumv">' + esc(numFmt.format(d.sum || 0)) + " ₽</span>" : '<span class="sumv"></span>') +
      (d.kind === "catalog"
        ? '<span class="badge ' + (d.deleted ? "draft" : "posted") + '">' + (d.deleted ? "отменён" : "справочник") + "</span>"
        : '<span class="badge ' + (d.posted ? "posted" : "draft") + '">' + (d.posted ? "проведён" : "черновик") + "</span>")));
    if (d._ref && d.uuid) {
      const btn = el("button", "btn view", "Посмотреть");
      btn.title = "Открыть в 1С";
      btn.onclick = () => openObject({ _ref: d._ref, uuid: d.uuid }, btn);
      card.appendChild(btn);
    }
    stepsEl = null;
  }

  // Открыть объект в 1С по кнопке на карточке. Идёт через тот же мост, что и инструменты,
  // но к серверу агента не относится: ответ 1С обрабатываем здесь же.
  let uiSeq = 0;
  const uiPending = {};
  function openObject(ref, btn) {
    if (standalone) { btn.textContent = "только в 1С"; return; }
    const id = "ui-" + (++uiSeq);
    uiPending[id] = (r) => {
      btn.textContent = r.ok ? "Открыто ✓" : "Ошибка";
      btn.title = r.ok ? "Открыть ещё раз" : (r.error || "");
      setTimeout(() => { btn.textContent = "Посмотреть"; }, 2500);
    };
    btn.textContent = "Открываю…";
    outbox.push({ type: "tool_call", id: id, name: "open_object", title: "открыть объект", args: { ref: ref } });
    flushOutbox();
  }

  // ---------- стартовый экран ----------
  const EXAMPLES = [
    { t: "Сколько счетов-фактур выдано в прошлом месяце?", i: "сводка по видам и суммам", send: true },
    { t: "Покажи пять последних реализаций и их суммы", i: "документы, контрагенты, итоги", send: true },
    { t: "Выпиши счёт покупателю ", i: "черновик, решение — за вами в окне 1С", send: false },
    { t: "Сверь реестр из файла с базой: что есть, что совпадает, чего нет", i: "прикрепите Excel кнопкой 📎", send: false },
  ];
  function welcome() {
    logEl.innerHTML = "";
    const w = el("div", "welcome",
      "<h1>◆ ИИ-консультант 1С</h1><p>Спрашивайте о данных базы — агент сам найдёт нужные документы и регистры " +
      "и покажет каждый шаг. Можно прикрепить скриншот ошибки или Excel.</p>");
    const ex = el("div", "ex");
    EXAMPLES.forEach((e) => {
      const b = el("button", "", esc(e.t) + "<i>" + esc(e.i) + "</i>");
      b.onclick = () => { input.value = e.t; autosize(); input.focus(); if (e.send) submit(); };
      ex.appendChild(b);
    });
    w.appendChild(ex);
    logEl.appendChild(w);
  }
  function clearWelcome() { const w = logEl.querySelector(".welcome"); if (w) w.remove(); }

  // ---------- мост 1С ----------
  window.bridgeHello = function (ctxJson) {
    const ctx = typeof ctxJson === "string" ? JSON.parse(ctxJson) : ctxJson;
    if (ctx.token !== undefined) { token = ctx.token || ""; delete ctx.token; }
    const first = context === null || standalone;
    standalone = false;
    context = ctx;
    if (denied && token) { denied = false; connect(); }  // токен ввели после отказа — пробуем снова
    else if (first) send(hello());
    renderMeta();
    return "ok";
  };
  window.bridgePop = function () {
    const ev = outbox.shift();
    return ev ? JSON.stringify(ev) : "";
  };
  // Очередь вызовов → #bridge-out по одному: 1С забирает и очищает поле. Переносим сразу,
  // как только вызов пришёл или 1С вернула результат, и «звоним» форме кликом по #bridge-ping;
  // интервал остаётся страховкой.
  function flushOutbox() {
    const out = $("bridge-out");
    if (!out.value && outbox.length) {
      out.value = bridgePop();
      $("bridge-ping").click();
    }
  }
  setInterval(flushOutbox, 150);
  $("bridge-btn").onclick = () => {
    const raw = $("bridge-in").value;
    $("bridge-in").value = "";
    const nl = raw.indexOf("\n");
    const kind = nl < 0 ? raw : raw.slice(0, nl), data = nl < 0 ? "" : raw.slice(nl + 1);
    if (kind === "hello") bridgeHello(data);
    else if (kind === "result") bridgeResult(data);
    else if (kind === "reset") bridgeReset();
    else if (kind === "settings") openSettings();
    // Не синхронно: 1С ещё внутри своей процедуры отправки — сигнал отдадим, когда она вернётся.
    setTimeout(flushOutbox, 0);
  };
  window.bridgeResult = function (json) {
    const r = typeof json === "string" ? JSON.parse(json) : json;
    if (uiPending[r.id]) { uiPending[r.id](r); delete uiPending[r.id]; return "ok"; } // кнопки интерфейса, не агент
    onToolResult(r);
    send({ type: "tool_result", id: r.id, ok: !!r.ok, result: r.result, error: r.error, summary: r.summary });
    return "ok";
  };
  function resetDialog() {
    send({ type: "reset" });
    attachments.length = 0; renderFiles();
    current = null; stepsEl = null; turn = null; hideThinking();
    welcome();
  }
  window.bridgeReset = function () { resetDialog(); return "ok"; };

  // ---------- вложения ----------
  const ICON = { image: "🖼", table: "📊", document: "📄", text: "📝" };
  function renderFiles() {
    const box = $("files");
    box.innerHTML = "";
    attachments.forEach((a, i) => {
      const chip = el("span", "chip " + a.state);
      const label = a.state === "uploading" ? "загрузка…" : a.state === "error" ? a.error : (a.parts > 1 ? a.parts + " ч." : "");
      chip.innerHTML = (ICON[a.kind] || "📎") + " " + esc(a.name) + (label ? " <i>" + esc(label) + "</i>" : "") + ' <b title="Убрать">✕</b>';
      chip.querySelector("b").onclick = () => { attachments.splice(i, 1); renderFiles(); };
      box.appendChild(chip);
    });
    box.style.display = attachments.length ? "flex" : "none";
  }
  async function uploadFile(file) {
    const a = { name: file.name || "скриншот.png", state: "uploading" };
    attachments.push(a); renderFiles();
    try {
      const form = new FormData();
      form.append("session", sessionId); // до файла: сервер читает поля по порядку
      form.append("file", file, a.name);
      const resp = await fetch(origin + "/upload", { method: "POST", body: form, headers: { "X-Agent-Token": token } });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || "ошибка загрузки");
      Object.assign(a, { id: data.file.id, kind: data.file.kind, parts: data.file.parts, state: "ready" });
    } catch (e) {
      Object.assign(a, { state: "error", error: String(e.message || e) });
    }
    renderFiles();
  }
  function addFiles(list) { Array.from(list || []).forEach(uploadFile); }
  $("attach").onclick = () => $("picker").click();
  $("picker").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };
  app.addEventListener("dragover", (e) => { e.preventDefault(); app.classList.add("drop"); });
  app.addEventListener("dragleave", (e) => { if (!e.relatedTarget || !app.contains(e.relatedTarget)) app.classList.remove("drop"); });
  app.addEventListener("drop", (e) => { e.preventDefault(); app.classList.remove("drop"); addFiles(e.dataTransfer.files); });
  document.addEventListener("paste", (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []).filter((it) => it.kind === "file");
    if (!items.length) return;
    e.preventDefault();
    items.forEach((it) => {
      const f = it.getAsFile();
      if (f) uploadFile(new File([f], f.name && f.name !== "image.png" ? f.name : "скриншот-" + new Date().toLocaleTimeString("ru").replace(/:/g, "-") + ".png", { type: f.type }));
    });
  });

  // ---------- команды и ввод ----------
  const HELP = [
    "**Команды**",
    "- `/новый` — начать новый диалог",
    "- `/тема` — переключить светлую / тёмную тему",
    "- `/помощь` — эта справка",
    "",
    "**Файлы**: 📎, перетащите в окно или вставьте скриншот через Ctrl+V. Скриншоты, Excel, PDF, Word, CSV, текст — до 20 МБ.",
    "",
    "**/тесты** — прогнать тестовый набор: числа сверяются с эталоном-запросом, поведение — по правилам (какие шаги, без лишних действий). `/тесты числа`, `/тесты поведение` — по отдельности; действия в прогоне не выполняются.",
    "",
    "Нажмите на шаг агента, чтобы увидеть запрос и результат. Действия, меняющие данные, подтверждаются в окне 1С.",
  ].join("\n");

  function toggleTheme() {
    const t = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    applyTheme(t); store.set("onec-agent-theme", t);
  }
  function showHelp() { clearWelcome(); const m = append(el("div", "msg sys md")); m.appendChild(md(HELP)); }

  function submit() {
    const ready = attachments.filter((a) => a.state === "ready");
    let text = input.value.trim();
    if (!text && !ready.length) return;
    if (/^\/(новый|new|clear)$/.test(text)) { input.value = ""; autosize(); resetDialog(); return; }
    if (/^\/(помощь|help)$/.test(text)) { input.value = ""; autosize(); showHelp(); return; }
    if (/^\/(тема|theme)$/.test(text)) { input.value = ""; autosize(); toggleTheme(); return; }
    if (busy) { append(el("div", "msg err", "Агент ещё работает — дождитесь ответа или нажмите «Стоп».")); return; }
    if (attachments.some((a) => a.state === "uploading")) { append(el("div", "msg err", "Дождитесь окончания загрузки файлов.")); return; }
    if (!text) text = "Посмотри прикреплённые файлы.";
    clearWelcome();
    input.value = ""; autosize();
    const names = ready.map((a) => (ICON[a.kind] || "📎") + " " + esc(a.name)).join("   ");
    append(el("div", "msg user", esc(text) + (names ? '<div class="att">' + names + "</div>" : "")));
    const evalCmd = /^\/(тесты|tests|evals)(?:\s+(\S+))?$/.exec(text);
    if (evalCmd) send({ type: "run_evals", only: evalCmd[2] || "" });
    else send({ type: "user_message", text: text, attachments: ready.map((a) => a.id) });
    attachments.length = 0; renderFiles();
    current = null; stepsEl = null;
    turn = { started: Date.now(), steps: 0 };
    setBusy(true);
    showThinking();
  }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && busy) send({ type: "interrupt" }); });
  $("send").onclick = submit;
  $("stop").onclick = () => send({ type: "interrupt" });
  $("theme").onclick = toggleTheme;
  $("gear").onclick = openSettings;

  // ---------- уточняющий вопрос агента: варианты ответа кнопками ----------
  function renderAsk(options) {
    if (!options.length) return;
    finishText();
    const box = append(el("div", "ask-opts"));
    options.forEach((text) => {
      const b = el("button", "btn ask-opt", esc(text));
      b.onclick = () => {
        if (busy) return;
        box.querySelectorAll("button").forEach((x) => { x.disabled = true; });
        b.classList.add("chosen");
        input.value = text; submit();
      };
      box.appendChild(b);
    });
  }

  // ---------- настройки модели ----------
  // Окно рисует страница, а хранит и проверяет профиль сервер агента (по базе). Ключ API сюда
  // не возвращается: поле пустое, «ключ задан» — значит, пустое поле оставит прежний.
  let settingsEl = null, providersInfo = {};
  function openSettings() { send({ type: "settings_get" }); }
  function closeSettings() { if (settingsEl) { settingsEl.remove(); settingsEl = null; } }
  function settingsStatus(kind, text) {
    if (!settingsEl) return;
    const st = settingsEl.querySelector(".set-status");
    st.className = "set-status " + kind; st.textContent = text;
  }
  function renderSettings(ev) {
    closeSettings();
    providersInfo = ev.providers;
    const p = ev.profile, ro = !ev.can_edit;
    const opts = Object.keys(providersInfo).map((k) =>
      '<option value="' + k + '"' + (k === p.provider ? " selected" : "") + ">" + esc(providersInfo[k].title) + "</option>").join("");
    settingsEl = el("div", "set-overlay",
      '<div class="set-box"><div class="set-title">Источник модели для этой базы</div>' +
      '<label>Источник<select data-f="provider"' + (ro ? " disabled" : "") + ">" + opts + "</select></label>" +
      '<label>Адрес API<input data-f="base_url" value="' + esc(p.base_url || "") + '"' + (ro ? " disabled" : "") + "></label>" +
      '<label>Модель<input data-f="model" value="' + esc(p.model || "") + '"' + (ro ? " disabled" : "") + "></label>" +
      '<label class="set-key">Ключ API<input data-f="api_key" type="password" autocomplete="off" placeholder="' +
        (p.has_key ? "ключ задан — оставьте пустым, чтобы не менять" : "ключ провайдера") + '"' + (ro ? " disabled" : "") + "></label>" +
      '<label class="set-gc">Scope GigaChat<select data-f="scope"' + (ro ? " disabled" : "") + ">" +
        ["GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"].map((v) =>
          '<option' + (v === (p.scope || "GIGACHAT_API_PERS") ? " selected" : "") + ">" + v + "</option>").join("") + "</select></label>" +
      '<label class="set-gc set-check"><input data-f="verify_ssl" type="checkbox"' + (p.verify_ssl === false ? "" : " checked") +
        (ro ? " disabled" : "") + "> Проверять сертификат сервера (снимите, если не установлен корневой сертификат Минцифры)</label>" +
      '<div class="set-cloud"><b>☁ Облачный провайдер.</b> Ваши вопросы и данные, которые агент читает из базы ' +
        "(суммы, контрагенты, номера документов, тексты ошибок), будут отправляться провайдеру и обрабатываться на его серверах." +
        '<label class="set-check"><input data-f="cloud_consent" type="checkbox"' + (ro ? " disabled" : "") +
        "> Я администратор и разрешаю передачу данных этой базы провайдеру</label></div>" +
      (p.consent_by ? '<div class="set-note">Облако разрешил: ' + esc(p.consent_by) + ", " + esc(p.consent_at || "") + "</div>" : "") +
      (ro ? '<div class="set-note">Менять настройки может только администратор базы.</div>' : "") +
      '<div class="set-status"></div><div class="set-actions">' +
      (ro ? "" : '<button class="btn" data-a="test">Проверить подключение</button><button class="btn primary" data-a="save">Сохранить</button>') +
      '<button class="btn" data-a="close">Закрыть</button></div></div>');
    document.body.appendChild(settingsEl);
    const f = (name) => settingsEl.querySelector('[data-f="' + name + '"]');
    function sync() {
      const info = providersInfo[f("provider").value] || {};
      settingsEl.querySelector(".set-key").hidden = !info.needs_key;
      settingsEl.querySelectorAll(".set-gc").forEach((n) => { n.hidden = f("provider").value !== "gigachat"; });
      settingsEl.querySelector(".set-cloud").hidden = !info.cloud;
    }
    f("provider").onchange = () => {
      const info = providersInfo[f("provider").value] || {};
      f("base_url").value = info.base_url || ""; f("model").value = info.model || "";
      f("api_key").placeholder = "ключ провайдера"; sync();
    };
    sync();
    function collect() {
      return { provider: f("provider").value, base_url: f("base_url").value.trim(), model: f("model").value.trim(),
        api_key: f("api_key").value, scope: f("scope").value, verify_ssl: f("verify_ssl").checked,
        cloud_consent: f("cloud_consent").checked };
    }
    settingsEl.onclick = (e) => {
      const a = e.target.getAttribute && e.target.getAttribute("data-a");
      if (e.target === settingsEl || a === "close") closeSettings();
      else if (a === "test") { settingsStatus("wait", "Проверяю подключение…"); send({ type: "settings_test", profile: collect() }); }
      else if (a === "save") { settingsStatus("wait", "Сохраняю…"); send({ type: "settings_save", profile: collect() }); }
    };
  }
  $("help").onclick = showHelp;

  function endTurn(interrupted) {
    finishText(); hideThinking();
    if (turn) {
      append(el("div", "turn-done", (interrupted ? "⏹ остановлено" : "✓ готово") + " · " + secs(Date.now() - turn.started) +
        (turn.steps ? " · шагов: " + turn.steps : "")));
      turn = null;
    }
    stepsEl = null;
    setBusy(false);
    input.focus();
  }

  // ---------- WebSocket ----------
  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      $("conn").classList.add("on");
      renderMeta();
      if (context) ws.send(JSON.stringify(hello()));
      while (pendingSend.length) {
        const m = pendingSend.shift();
        if (m.type !== "hello") ws.send(JSON.stringify(m));
      }
    };
    ws.onclose = () => {
      $("conn").classList.remove("on");
      if (denied) { $("meta").textContent = "нет доступа: неверный токен"; return; }
      $("meta").textContent = "нет связи с сервером, переподключение…";
      if (busy && unanswered) { if (!pendingSend.includes(unanswered)) pendingSend.push(unanswered); }
      else if (busy) endTurn(true);
      setTimeout(connect, 2000);
    };
    ws.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type !== "ready") unanswered = null;
      switch (ev.type) {
        case "ready": model = ev.model || ""; renderMeta(); break;
        case "auth_error": denied = true; append(el("div", "msg err", esc(ev.message))); break;
        case "ask": renderAsk(ev.options || []); break;
        case "model": modelInfo = ev; $("gear").hidden = !ev.can_edit; renderMeta(); break;
        case "settings": renderSettings(ev); break;
        case "settings_test": settingsStatus(ev.ok ? "ok" : "err", ev.ok
          ? "Подключение работает: «" + ev.reply + "» за " + ev.seconds + " с" : "Ошибка: " + ev.error); break;
        case "settings_error": settingsStatus("err", ev.message); break;
        case "settings_saved": settingsStatus("ok", "Сохранено: " + ev.profile.label); setTimeout(closeSettings, 1200); break;
        case "text_delta": onTextDelta(ev.text); break;
        case "text_end": finishText(); break;
        case "tool_call": onToolCall(ev); break;
        case "tool_result": onToolResult(ev); break;
        case "error": finishText(); hideThinking(); append(el("div", "msg err", esc(ev.message))); break;
        case "turn_done": endTurn(ev.interrupted); break;
        case "reset_done": setBusy(false); break;
      }
    };
  }

  // Если за 3 с 1С не прислала hello — работаем как обычная страница (/dev).
  setTimeout(() => {
    if (context === null) {
      standalone = true;
      context = { client: "браузер (dev, без 1С)", today: new Date().toISOString().slice(0, 10) };
      send(hello());
      renderMeta();
    }
  }, 3000);
  welcome();
  connect();
  input.focus();
  // Метка для формы 1С: функции моста определены, их можно вызывать.
  document.title = "onec-agent-ready";
})();
