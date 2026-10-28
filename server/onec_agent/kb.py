"""База знаний: поиск по своим инструкциям, регламентам и решённым обращениям (kb_search).

Документы кладутся в каталог KB_DIR (md, txt, docx, pdf, html); решённые обращения —
в подкаталог tickets/, по файлу на обращение. Индекс строится в памяти при первом
поиске и пересобирается, когда файлы меняются. Поиск лексический (BM25 по основам слов):
без эмбеддинг-модели, без внешних сервисов, данные не покидают сервер.
"""
import math
import re
from collections import Counter
from pathlib import Path

from . import config, files

CHUNK_CHARS = 1500
EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".html", ".htm"}

_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
# Окончания по убыванию длины: грубый стеммер, но для поиска по инструкциям его хватает.
_ENDINGS = sorted("""
ами ями ого его ому ему ыми ими ией иям иях ения ение ений ениям ениях ться тся ешь ете ишь ите
ует уют ают яют ала ила ыла ела али или ыли ели ать ять ить еть уть ом ем ой ей ую юю ая яя ое ее
ые ие ый ий ов ев ам ям ах ях ую ть ся а я о е ы и у ю ь й
""".split(), key=len, reverse=True)


def _stem(word: str) -> str:
    word = word.lower().replace("ё", "е")
    if len(word) <= 4 or not re.match(r"[а-я]", word):
        return word
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[: -len(end)]
    return word


# Служебные слова вопросов («что делать», «как у нас») — не смысл. «Не» оставлено: в текстах ошибок оно важно.
_STOP = set("что как это так там тут где когда если или для при по на за из от до во со ко об у в и а с о к "
            "мы нас нам вы вас вам он она они его её их мой наш ваш быть есть был была было были делать сделать "
            "надо нужно можно почему зачем какой какая какие который которая".split())


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text) if len(w) > 1 and w.lower().replace("ё", "е") not in _STOP]


def _chunks(text: str) -> list[str]:
    """Режем по абзацам, держась заголовков: кусок — не больше CHUNK_CHARS."""
    out, cur = [], ""
    for block in re.split(r"\n\s*\n", text):
        if cur and (len(cur) + len(block) > CHUNK_CHARS or block.lstrip().startswith("#")):
            out.append(cur.strip())
            cur = ""
        cur += block + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def _read(path: Path) -> str:
    ext = path.suffix.lower()
    data = path.read_bytes()
    if ext == ".pdf":
        return "\n\n".join(p["text"] for p in files._extract_pdf(data))
    if ext == ".docx":
        return "\n\n".join(p["text"] for p in files._extract_docx(data))
    text = files._decode(data)
    if ext in (".html", ".htm"):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _title(path: Path, text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return path.stem


class _Index:
    def __init__(self):
        self.signature = None
        self.docs: list[dict] = []
        self.df: Counter = Counter()
        self.avg_len = 1.0

    def refresh(self, root: Path) -> None:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
                       and p.name.lower() != "readme.md")
        signature = tuple((str(p), p.stat().st_mtime) for p in paths)
        if signature == self.signature:
            return
        docs = []
        for path in paths:
            try:
                text = _read(path)
            except Exception as exc:  # битый файл не должен ронять поиск по остальным
                text = f"(не удалось прочитать: {exc})"
            rel = path.relative_to(root)
            kind = "ticket" if rel.parts[0] == "tickets" else "doc"
            title = _title(path, text)
            for i, chunk in enumerate(_chunks(text)):
                tokens = _tokens(title + " " + chunk)
                docs.append({"file": str(rel), "kind": kind, "title": title, "part": i + 1,
                             "text": chunk, "tf": Counter(tokens), "len": len(tokens),
                             "pairs": set(zip(tokens, tokens[1:]))})
        self.docs = docs
        self.df = Counter(t for d in docs for t in d["tf"])
        self.avg_len = sum(d["len"] for d in docs) / len(docs) if docs else 1.0
        self.signature = signature

    def search(self, query: str, kind: str | None, limit: int) -> list[tuple[float, dict]]:
        query_tokens = _tokens(query)
        terms = set(query_tokens)
        # Совпадение соседних пар слов — признак цитаты (текст ошибки, название операции): весит больше слов.
        pairs = set(zip(query_tokens, query_tokens[1:]))
        n = len(self.docs)
        scored = []
        for d in self.docs:
            if kind and d["kind"] != kind:
                continue
            score = 0.0
            for t in terms:
                tf = d["tf"].get(t)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * d["len"] / self.avg_len))
            score += 2.0 * len(pairs & d["pairs"])
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]


_index = _Index()


def search(query: str, kind: str | None = None, limit: int = 5) -> dict:
    root = Path(config.KB_DIR)
    if not root.is_dir():
        return {"total": 0, "results": [], "note": "База знаний пуста: положите инструкции в server/kb, обращения — в server/kb/tickets"}
    _index.refresh(root)
    kind = {"tickets": "ticket", "ticket": "ticket", "docs": "doc", "doc": "doc"}.get(kind or "")
    hits = _index.search(query, kind, limit)
    return {
        "total": len(hits),
        "results": [{"source": d["file"] + (f" (часть {d['part']})" if d["part"] > 1 else ""),
                     "kind": "решённое обращение" if d["kind"] == "ticket" else "документ",
                     "title": d["title"], "score": round(s, 2), "text": d["text"][:CHUNK_CHARS]}
                    for s, d in hits],
        **({} if hits else {"note": "Ничего не найдено — переформулируй другими словами или скажи пользователю, что в базе знаний этого нет"}),
    }
