"""Встроенная локальная модель: llama.cpp (llama-server) под управлением приложения.

Скачивает GGUF с Hugging Face (докачка, проверка SHA-256), запускает и останавливает llama-server
с базовыми настройками (контекст, параллельность, выгрузка при простое) и отдаёт его адрес как
OpenAI-совместимый endpoint — провайдер «builtin» в providers.py.
"""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import aiohttp

from . import config

log = logging.getLogger("onec_agent")

# Готовые варианты: что качать и сколько памяти нужно, чтобы модель работала, а не свопилась.
PRESETS = {
    "qwen3-30b-a3b": {
        "title": "Qwen3-30B-A3B Instruct 2507 (Q4_K_M) — основная",
        "repo": "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF",
        "file": "Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
        "size_gb": 18.6, "min_ram_gb": 32, "ctx": 32768,
        "note": "Проверена тестами консультанта: 34 из 36 на демо-базе.",
    },
    "qwen3-4b": {
        "title": "Qwen3-4B Instruct 2507 (Q4_K_M) — лёгкая",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "size_gb": 2.5, "min_ram_gb": 8, "ctx": 16384,
        "note": "Для слабых машин и проверки: заметно чаще ошибается в запросах и инструментах.",
    },
}

DEFAULTS = {
    "preset": "qwen3-30b-a3b",
    "ctx": 32768,           # контекст: промпт консультанта + история + результаты инструментов
    "parallel": 1,          # один запрос за раз — вся память под контекст одного диалога
    "gpu_layers": 99,       # всё на GPU (Metal/CUDA); 0 — только процессор
    "idle_unload_min": 30,  # выгрузить модель после простоя, минут (0 — не выгружать)
    "autostart": True,      # поднимать модель при первом запросе
}


class LocalModelError(Exception):
    pass


def _settings_path() -> Path:
    return Path(config.LOCAL_MODEL_SETTINGS)


def settings() -> dict:
    path = _settings_path()
    data = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(changes: dict) -> dict:
    current = settings()
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise LocalModelError(f"Неизвестная настройка {key}")
        if key == "preset" and value not in PRESETS:
            raise LocalModelError(f"Неизвестная модель {value}")
        if key in ("ctx", "parallel", "gpu_layers", "idle_unload_min"):
            value = int(value)
            if key == "ctx" and not 2048 <= value <= 262144:
                raise LocalModelError("Контекст — от 2048 до 262144 токенов")
            if key == "parallel" and not 1 <= value <= 8:
                raise LocalModelError("Параллельных запросов — от 1 до 8")
        if key == "autostart":
            value = bool(value)
        current[key] = value
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=1), "utf-8")
    return current


def model_path(preset: str) -> Path:
    return Path(config.MODELS_DIR) / PRESETS[preset]["file"]


def find_server_binary() -> str | None:
    """llama-server: путь из настройки, рядом с приложением (bin/), иначе из PATH."""
    candidates = [config.LLAMA_SERVER, str(Path(config.APP_DIR) / "bin" / "llama-server"), shutil.which("llama-server")]
    return next((c for c in candidates if c and Path(c).is_file() and os.access(c, os.X_OK)), None)


def ram_gb() -> float:
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
        return int(out) / 2**30
    except Exception:
        return 0.0


def _rss_mb(pid: int) -> int:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip() or 0) // 1024
    except Exception:
        return 0


class Manager:
    """Одна модель за раз. Состояние читает панель приложения и трей."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.loaded_preset: str | None = None
        self.state = "stopped"          # stopped | starting | ready | error
        self.error = ""
        self.last_used = 0.0
        self.download: dict | None = None  # {"preset", "done", "total", "speed", "state", "error"}
        self._download_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def port(self) -> int:
        return config.LLAMA_PORT

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    # ---------- состояние ----------

    def status(self) -> dict:
        s = settings()
        running = self.proc is not None and self.proc.poll() is None
        if self.proc is not None and not running and self.state in ("ready", "starting"):
            self.state, self.error = "error", f"llama-server завершился (код {self.proc.returncode}), см. logs/llama.log"
        presets = []
        for key, p in PRESETS.items():
            path = model_path(key)
            # Готовый файл появляется только после проверки контрольной суммы (до этого — .part).
            presets.append({"id": key, "title": p["title"], "size_gb": p["size_gb"],
                            "min_ram_gb": p["min_ram_gb"], "note": p["note"], "downloaded": path.is_file()})
        return {
            "state": self.state if running or self.state == "error" else "stopped",
            "error": self.error, "preset": self.loaded_preset if running else None,
            "endpoint": self.base_url, "memory_mb": _rss_mb(self.proc.pid) if running else 0,
            "idle_sec": int(time.time() - self.last_used) if running and self.last_used else None,
            "binary": find_server_binary(), "ram_gb": round(ram_gb()), "settings": s,
            "presets": presets, "download": self.download,
        }

    # ---------- загрузка модели ----------

    def start_download(self, preset: str) -> None:
        if preset not in PRESETS:
            raise LocalModelError(f"Неизвестная модель {preset}")
        if self._download_task and not self._download_task.done():
            raise LocalModelError("Уже идёт загрузка — дождитесь или отмените")
        self.download = {"preset": preset, "done": 0, "total": int(PRESETS[preset]["size_gb"] * 1e9), "speed": 0,
                         "state": "running", "error": ""}
        self._download_task = asyncio.create_task(self._download(preset))

    def cancel_download(self) -> None:
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()

    async def _download(self, preset: str) -> None:
        p = PRESETS[preset]
        target = model_path(preset)
        part = target.with_suffix(".gguf.part")
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{p['repo']}/resolve/main/{p['file']}"
        try:
            meta = await _file_meta(p)  # точный размер и SHA-256 — из метаданных Hugging Face
            if not meta:
                raise LocalModelError("Нет связи с Hugging Face — проверьте интернет и повторите")
            size, expected = meta
            self.download["total"] = size
            done = part.stat().st_size if part.exists() else 0
            if done > size:
                part.unlink()
                done = 0
            started, window_bytes, window_start = time.time(), 0, time.time()
            if done < size:
                headers = {"Range": f"bytes={done}-"} if done else {}
                timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(url, headers=headers) as resp:
                        if resp.status == 200 and done:
                            done = 0  # сервер не умеет докачку — начинаем заново
                        elif resp.status not in (200, 206):
                            raise LocalModelError(f"Hugging Face ответил {resp.status}")
                        with open(part, "ab" if done else "wb") as out:
                            async for chunk in resp.content.iter_chunked(1 << 20):
                                out.write(chunk)
                                done += len(chunk)
                                window_bytes += len(chunk)
                                if time.time() - window_start >= 1:
                                    self.download["speed"] = int(window_bytes / (time.time() - window_start))
                                    window_bytes, window_start = 0, time.time()
                                self.download["done"] = done
            self.download["done"] = done
            if done != size:
                raise LocalModelError(f"Скачано {done} байт из {size} — запустите загрузку ещё раз, она продолжится")
            self.download["state"] = "verifying"
            digest = await asyncio.to_thread(_sha256, part)
            if expected and digest != expected:
                part.unlink(missing_ok=True)
                raise LocalModelError("Контрольная сумма не совпала — файл удалён, скачайте заново")
            part.replace(target)
            self.download["state"] = "done"
            log.info("модель скачана: %s (%.1f ГБ за %d с)", target.name, size / 1e9, time.time() - started)
        except asyncio.CancelledError:
            self.download["state"] = "cancelled"
            raise
        except Exception as exc:
            self.download.update(state="error", error=str(exc))
            log.warning("загрузка модели: %s", exc)

    # ---------- запуск ----------

    async def ensure_running(self) -> str:
        """Для шлюза: модель должна отвечать. Поднимает её при автозапуске; возвращает endpoint."""
        self.last_used = time.time()
        if self.state == "ready" and self.proc and self.proc.poll() is None:
            return self.base_url
        if not settings()["autostart"]:
            raise LocalModelError("Встроенная модель остановлена — запустите её в приложении onec-agent")
        await self.start()
        return self.base_url

    async def start(self, preset: str | None = None) -> None:
        async with self._lock:
            s = settings()
            preset = preset or s["preset"]
            if self.proc and self.proc.poll() is None:
                if self.loaded_preset == preset:
                    return
                await self._stop()
            binary = find_server_binary()
            if not binary:
                raise LocalModelError("Не найден llama-server (llama.cpp). Установите его или положите в bin/ приложения")
            path = model_path(preset)
            if not path.is_file():
                raise LocalModelError(f"Модель {PRESETS[preset]['title']} не скачана — скачайте её в приложении")
            if ram_gb() and ram_gb() < PRESETS[preset]["min_ram_gb"]:
                log.warning("памяти %d ГБ меньше рекомендованных %d ГБ для %s", ram_gb(), PRESETS[preset]["min_ram_gb"], preset)
            if _port_busy(self.port):
                raise LocalModelError(f"Порт {self.port} уже занят другой программой (вторая копия приложения или "
                                      "чужой llama-server) — остановите её или задайте ONEC_AGENT_LLAMA_PORT")
            args = [binary, "-m", str(path), "--host", "127.0.0.1", "--port", str(self.port),
                    "-c", str(s["ctx"]), "--parallel", str(s["parallel"]), "-ngl", str(s["gpu_layers"]),
                    "--jinja",  # шаблон чата модели: без него не работает вызов инструментов
                    "--alias", preset, "--no-webui"]
            Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
            logfile = open(Path(config.LOG_DIR) / "llama.log", "ab")
            self.state, self.error = "starting", ""
            self.proc = subprocess.Popen(args, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            self.loaded_preset = preset
            log.info("llama-server запущен: %s, контекст %s", preset, s["ctx"])
        await self._wait_ready()

    async def _wait_ready(self, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            while time.time() < deadline:
                if self.proc is None or self.proc.poll() is not None:
                    self.state = "error"
                    self.error = "llama-server не запустился — см. logs/llama.log (часто: не хватает памяти)"
                    raise LocalModelError(self.error)
                try:
                    async with http.get(f"http://127.0.0.1:{self.port}/health") as resp:
                        if resp.status == 200:
                            self.state, self.last_used = "ready", time.time()
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(1)
        self.state, self.error = "error", "Модель не загрузилась за 5 минут"
        raise LocalModelError(self.error)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(50):
                if self.proc.poll() is not None:
                    break
                await asyncio.sleep(0.2)
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
            log.info("llama-server остановлен")
        self.proc, self.loaded_preset, self.state = None, None, "stopped"

    async def idle_watch(self) -> None:
        """Выгрузка при простое: модель держит 18+ ГБ памяти, а консультантом пользуются урывками."""
        while True:
            await asyncio.sleep(30)
            minutes = settings()["idle_unload_min"]
            if minutes and self.state == "ready" and self.last_used and time.time() - self.last_used > minutes * 60:
                log.info("модель выгружается после %d мин простоя", minutes)
                await self.stop()


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
    return h.hexdigest()


async def _file_meta(p: dict) -> tuple[int, str | None] | None:
    """Размер и SHA-256 (LFS oid) файла модели по метаданным Hugging Face."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.get(f"https://huggingface.co/api/models/{p['repo']}/tree/main") as resp:
                for f in await resp.json():
                    if f.get("path") == p["file"]:
                        lfs = f.get("lfs") or {}
                        return int(lfs.get("size") or f["size"]), lfs.get("oid")
    except Exception:
        return None
    return None


manager = Manager()
