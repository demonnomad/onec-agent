"""Базы 1С для панели приложения: список и выгрузка исходников в базу (extension/load-sources.sh).

Список — файловые базы из списка баз 1С этого компьютера (ibases.v8i) и добавленные вручную
(data/bases.json). Выгрузка идёт фоновой задачей; если база опубликована автономным сервером
ibsrv, он на время выгрузки останавливается (ibcmd нужна монопольная работа) и запускается снова.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from . import config

log = logging.getLogger("onec_agent")

IBASES_FILES = [Path.home() / ".1C" / "1cestart" / "ibases.v8i",
                Path.home() / "Library" / "Application Support" / "1C" / "1cestart" / "ibases.v8i",
                Path(os.environ.get("APPDATA", "")) / "1C" / "1CEStart" / "ibases.v8i"]
LOAD_SCRIPT = Path(config.APP_DIR).parent / "extension" / "load-sources.sh"


class BaseError(Exception):
    pass


def _base_id(path: str) -> str:
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:12]


def _added_path() -> Path:
    return Path(config.BASES_FILE)


def _added() -> list[dict]:
    path = _added_path()
    return json.loads(path.read_text("utf-8")) if path.is_file() else []


def _from_ibases() -> list[dict]:
    """[Имя] … Connect=File="путь"; — только файловые базы: выгрузку для серверных пока не делаем."""
    found = []
    for path in IBASES_FILES:
        if not path.is_file():
            continue
        name = None
        for line in path.read_text("utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                name = line[1:-1]
            m = re.match(r'Connect=File="([^"]+)"', line)
            if m and name:
                found.append({"title": name, "path": m.group(1), "user": "", "source": "список баз 1С"})
    return found


def list_bases() -> list[dict]:
    seen, out = set(), []
    for base in _added() + _from_ibases():
        bid = _base_id(base["path"])
        if bid in seen:
            continue
        seen.add(bid)
        exists = (Path(base["path"]) / "1Cv8.1CD").is_file()
        published = _ibsrv_for(base["path"])
        out.append({**base, "id": bid, "exists": exists,
                    "published": published["config"] if published else None,
                    "last_export": _last_export().get(bid)})
    return out


def add_base(path: str, title: str = "", user: str = "") -> dict:
    path = os.path.expanduser(path.strip())
    if not (Path(path) / "1Cv8.1CD").is_file():
        raise BaseError(f"В «{path}» нет файловой базы 1С (файла 1Cv8.1CD)")
    added = [b for b in _added() if _base_id(b["path"]) != _base_id(path)]
    added.append({"title": title.strip() or Path(path).name, "path": path, "user": user.strip(), "source": "добавлена вручную"})
    _added_path().parent.mkdir(parents=True, exist_ok=True)
    _added_path().write_text(json.dumps(added, ensure_ascii=False, indent=1), "utf-8")
    return next(b for b in list_bases() if b["id"] == _base_id(path))


def _find(base_id: str) -> dict:
    base = next((b for b in list_bases() if b["id"] == base_id), None)
    if base is None:
        raise BaseError("Нет такой базы в списке")
    if not base["exists"]:
        raise BaseError(f"База «{base['title']}» не найдена на диске: {base['path']}")
    return base


# ---------- автономный сервер ibsrv ----------

def _ibsrv_for(db_path: str) -> dict | None:
    """Процесс ibsrv, который держит эту базу: ищем по --config, в нём database.path."""
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    target = os.path.realpath(db_path)
    for line in ps.splitlines():
        if "ibsrv" not in line or "--config=" not in line:
            continue
        pid, command = line.strip().split(" ", 1)
        conf = re.search(r"--config=(\S+)", command).group(1)
        try:
            text = Path(conf).read_text("utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"database:\s*\n\s*path:\s*(.+)", text)
        if m and os.path.realpath(m.group(1).strip()) == target:
            return {"pid": int(pid), "command": command, "config": conf}
    return None


async def _stop_ibsrv(info: dict, say) -> None:
    say(f"→ останавливаю публикацию ibsrv ({Path(info['config']).parent.name}) на время выгрузки")
    os.kill(info["pid"], signal.SIGTERM)
    for _ in range(60):
        await asyncio.sleep(0.5)
        try:
            os.kill(info["pid"], 0)
        except ProcessLookupError:
            return
    os.kill(info["pid"], signal.SIGKILL)  # ibsrv порой висит после «Stand-alone Server stopped»
    await asyncio.sleep(1)


def _start_ibsrv(info: dict, say) -> None:
    say("→ запускаю публикацию ibsrv обратно")
    workdir = Path(info["config"]).parent
    with open(workdir / "ibsrv.log", "ab") as logf:
        subprocess.Popen(info["command"].split(" "), cwd=workdir, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


# ---------- выгрузка ----------

_jobs: dict[str, dict] = {}


def _last_export_path() -> Path:
    return Path(config.BASES_FILE).with_name("exports.json")


def _last_export() -> dict:
    path = _last_export_path()
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


def jobs() -> dict:
    return {k: {kk: vv for kk, vv in v.items() if kk != "task"} for k, v in _jobs.items()}


def start_export(base_id: str) -> dict:
    base = _find(base_id)
    if base_id in _jobs and _jobs[base_id]["state"] == "running":
        raise BaseError("Выгрузка этой базы уже идёт")
    if any(j["state"] == "running" for j in _jobs.values()):
        raise BaseError("Идёт выгрузка другой базы — дождитесь окончания (она занимает много памяти и диска)")
    job = {"base": base["title"], "state": "running", "started": time.time(), "log": [], "result": ""}
    _jobs[base_id] = job
    job["task"] = asyncio.create_task(_export(base, job))
    return jobs()[base_id]


async def _export(base: dict, job: dict) -> None:
    def say(line: str) -> None:
        job["log"] = (job["log"] + [line])[-40:]

    ibsrv = _ibsrv_for(base["path"])
    try:
        if ibsrv:
            await _stop_ibsrv(ibsrv, say)
        args = [str(LOAD_SCRIPT), base["path"]] + ([base["user"]] if base.get("user") else [])
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip().lstrip("﻿")
            if line:
                say(line)
        code = await proc.wait()
        result = next((l for l in reversed(job["log"]) if "модулей" in l), "")
        if code != 0 or not result or any(l.startswith(("ОШИБКА", "✗")) for l in job["log"]):
            raise BaseError(next((l for l in reversed(job["log"]) if l.startswith(("ОШИБКА", "✗", "[ERROR]"))),
                                 f"скрипт выгрузки завершился с кодом {code}"))
        job.update(state="done", result=result)
        record = _last_export()
        record[_base_id(base["path"])] = {"at": time.strftime("%Y-%m-%d %H:%M"), "result": result}
        _last_export_path().parent.mkdir(parents=True, exist_ok=True)
        _last_export_path().write_text(json.dumps(record, ensure_ascii=False, indent=1), "utf-8")
    except Exception as exc:
        job.update(state="error", result=str(exc)[:500])
        log.warning("выгрузка %s: %s", base["title"], exc)
    finally:
        if ibsrv:
            _start_ibsrv(ibsrv, say)
        job["finished"] = time.time()
