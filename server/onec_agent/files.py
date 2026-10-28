"""Вложения чата: приём, извлечение текста, хранение по диалогам.

Файл режется на «части» (страница PDF, кусок листа Excel, кусок текста), чтобы агент
читал его инструментами по частям, а не целиком в промпт: контекст локальной модели мал.

Раскладка на диске: <UPLOAD_DIR>/<сессия>/<id>/{original.<ext>, meta.json, parts.json}.
Сессия — случайный идентификатор диалога со стороны страницы; знание его даёт доступ
к файлам, поэтому он генерируется криптостойко и нигде не логируется.
"""
import asyncio
import base64
import csv
import datetime as dt
import io
import json
import re
import secrets
import shutil
import time
from pathlib import Path

import aiohttp

from . import config

TEXT_EXT = {".txt", ".csv", ".tsv", ".xml", ".json", ".bsl", ".os", ".md", ".log", ".sql", ".html", ".htm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOC_EXT = {".pdf", ".docx", ".xlsx", ".xlsm"}
ALLOWED_EXT = TEXT_EXT | IMAGE_EXT | DOC_EXT

PART_CHARS = 3000      # размер текстовой части
SHEET_ROWS = 80        # строк Excel в одной части
PREVIEW_CHARS = 1200   # сколько показать агенту сразу при прикреплении

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ID_RE = re.compile(r"^f[0-9a-f]{8}$")

VISION_PROMPT = (
    "Это скриншот из программы 1С:Предприятие или документа. Сначала перепиши ДОСЛОВНО весь текст, "
    "который видишь (сообщения об ошибках, заголовки окон, поля и их значения, таблицы — построчно). "
    "Потом одной-двумя фразами опиши, что изображено. Ничего не додумывай."
)


class FileError(Exception):
    pass


def root() -> Path:
    return Path(config.UPLOAD_DIR)


def _session_dir(session: str) -> Path:
    if not _SESSION_RE.match(session or ""):
        raise FileError("некорректный идентификатор диалога")
    return root() / session


def new_file_id() -> str:
    return "f" + secrets.token_hex(4)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _split_text(text: str, label: str = "") -> list[dict]:
    parts, chunk = [], []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > PART_CHARS and chunk:
            parts.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line)
    if chunk:
        parts.append("".join(chunk))
    return [{"title": f"{label}часть {i + 1}", "text": p} for i, p in enumerate(parts or [""])]


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.strftime("%d.%m.%Y %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%d.%m.%Y")
    if isinstance(v, dt.date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).replace("\t", " ").replace("\n", " ")


def _extract_xlsx(data: bytes) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None and str(c).strip() for c in r)]
        if not rows:
            continue
        header = "\t".join(_cell(c) for c in rows[0])
        body = rows[1:] or [()]
        for start in range(0, len(body), SHEET_ROWS):
            chunk = body[start:start + SHEET_ROWS]
            lines = [header] + ["\t".join(_cell(c) for c in r) for r in chunk]
            parts.append({
                "title": f"лист «{ws.title}», строки {start + 2}–{start + 1 + len(chunk)} из {len(body) + 1}",
                "text": "\n".join(lines),
            })
    return parts or [{"title": "пусто", "text": ""}]


def _extract_pdf(data: bytes) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        parts.append({"title": f"страница {i + 1} из {len(reader.pages)}", "text": text or "(на странице нет текстового слоя — возможно, это скан)"})
    return parts or [{"title": "пусто", "text": ""}]


def _extract_docx(data: bytes) -> list[dict]:
    from docx import Document
    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for ti, table in enumerate(doc.tables):
        lines.append(f"\n[Таблица {ti + 1}]")
        for row in table.rows:
            lines.append("\t".join(c.text.strip().replace("\n", " ") for c in row.cells))
    return _split_text("\n".join(lines))


def _extract_csv(text: str) -> list[dict]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        return _split_text(text)
    if not rows:
        return _split_text(text)
    header, body = "\t".join(rows[0]), rows[1:] or [[]]
    return [
        {"title": f"строки {s + 2}–{s + 1 + len(body[s:s + SHEET_ROWS])} из {len(body) + 1}",
         "text": "\n".join([header] + ["\t".join(r) for r in body[s:s + SHEET_ROWS]])}
        for s in range(0, len(body), SHEET_ROWS)
    ]


async def _describe_image(http: aiohttp.ClientSession, data: bytes) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))  # крупные скриншоты ужимаем: качество распознавания не падает, токенов меньше
    buf = io.BytesIO()
    img.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    payload = {
        "model": config.VISION_MODEL,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    async with http.post(f"{config.VISION_BASE_URL}/chat/completions", json=payload, headers=headers) as resp:
        if resp.status != 200:
            raise FileError(f"модель распознавания недоступна (HTTP {resp.status})")
        body = await resp.json()
    return body["choices"][0]["message"]["content"].strip()


async def save_upload(http: aiohttp.ClientSession, session: str, filename: str, data: bytes) -> dict:
    """Сохраняет файл, извлекает текст, возвращает метаданные для интерфейса и агента."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise FileError(f"тип файла {ext or '(без расширения)'} не поддерживается")
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise FileError(f"файл больше {config.UPLOAD_MAX_BYTES // (1024 * 1024)} МБ")

    if ext in IMAGE_EXT:
        kind = "image"
        parts = [{"title": "распознанный текст скриншота", "text": await _describe_image(http, data)}]
    elif ext in (".xlsx", ".xlsm"):
        kind = "table"
        parts = await asyncio.to_thread(_extract_xlsx, data)
    elif ext == ".pdf":
        kind = "document"
        parts = await asyncio.to_thread(_extract_pdf, data)
    elif ext == ".docx":
        kind = "document"
        parts = await asyncio.to_thread(_extract_docx, data)
    elif ext in (".csv", ".tsv"):
        kind = "table"
        parts = _extract_csv(_decode(data))
    else:
        kind = "text"
        parts = _split_text(_decode(data))

    file_id = new_file_id()
    folder = _session_dir(session) / file_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"original{ext}").write_bytes(data)
    meta = {
        "id": file_id,
        "name": Path(filename).name,
        "kind": kind,
        "size": len(data),
        "parts": len(parts),
        "uploaded": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    (folder / "parts.json").write_text(json.dumps(parts, ensure_ascii=False), "utf-8")
    return meta


def load(session: str, file_id: str) -> tuple[dict, list[dict]]:
    if not _ID_RE.match(file_id or ""):
        raise FileError(f"нет файла {file_id}")
    folder = _session_dir(session) / file_id
    if not (folder / "meta.json").is_file():
        raise FileError(f"нет файла {file_id} в этом диалоге")
    meta = json.loads((folder / "meta.json").read_text("utf-8"))
    parts = json.loads((folder / "parts.json").read_text("utf-8"))
    return meta, parts


def list_files(session: str) -> list[dict]:
    folder = _session_dir(session)
    if not folder.is_dir():
        return []
    return [json.loads((d / "meta.json").read_text("utf-8")) for d in sorted(folder.iterdir()) if (d / "meta.json").is_file()]


def attachment_note(session: str, file_ids: list[str]) -> str:
    """Текст для агента о прикреплённых к сообщению файлах: что это и превью начала."""
    blocks = []
    for fid in file_ids:
        try:
            meta, parts = load(session, fid)
        except FileError:
            continue
        head = f"[Файл {meta['id']}] «{meta['name']}» — {meta['kind']}, частей: {meta['parts']}"
        preview = parts[0]["text"] if parts else ""
        if meta["kind"] == "image":
            blocks.append(f"{head}\n{preview}")  # распознанный скриншот короткий — отдаём целиком
        else:
            cut = preview[:PREVIEW_CHARS]
            more = "\n…(дальше — инструментами file_read / file_search)" if len(preview) > PREVIEW_CHARS or meta["parts"] > 1 else ""
            blocks.append(f"{head}\nНачало ({parts[0]['title']}):\n{cut}{more}")
    if not blocks:
        return ""
    return "\n\nПрикреплённые файлы:\n" + "\n\n".join(blocks)


def read_part(session: str, file_id: str, part: int) -> dict:
    meta, parts = load(session, file_id)
    if part < 1 or part > len(parts):
        raise FileError(f"у файла {meta['name']} части 1–{len(parts)}")
    p = parts[part - 1]
    return {"file": meta["name"], "part": part, "parts_total": len(parts), "title": p["title"], "text": p["text"]}


def search(session: str, query: str, file_id: str | None = None, limit: int = 20) -> dict:
    """Поиск строк, где встречаются все слова запроса (без учёта регистра и «ё»)."""
    norm = lambda s: s.lower().replace("ё", "е")
    words = [w for w in norm(query).split() if w]
    if not words:
        raise FileError("пустой запрос")
    metas = [load(session, file_id)[0]] if file_id else list_files(session)
    hits, total = [], 0
    for meta in metas:
        _, parts = load(session, meta["id"])
        for pi, p in enumerate(parts, 1):
            for line in p["text"].splitlines():
                if all(w in norm(line) for w in words):
                    total += 1
                    if len(hits) < limit:
                        hits.append({"file": meta["id"], "name": meta["name"], "part": pi, "line": line.strip()[:400]})
    return {"total": total, "hits": hits}


def drop_session(session: str) -> None:
    try:
        shutil.rmtree(_session_dir(session), ignore_errors=True)
    except FileError:
        pass


def cleanup_old(max_age_hours: int) -> None:
    """Удаляет диалоги, к которым не обращались дольше max_age_hours (при старте сервера)."""
    if not root().is_dir():
        return
    limit = time.time() - max_age_hours * 3600
    for d in root().iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)
