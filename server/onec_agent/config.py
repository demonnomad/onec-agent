"""Настройки сервера. Всё переопределяется переменными окружения ONEC_AGENT_*.

Каталоги: APP_DIR — где живут данные (data/, logs/): в исходниках это server/, у собранного одним
файлом сервера (PyInstaller) — каталог рядом с исполняемым файлом. RES_DIR — ресурсы, упакованные
внутрь сборки (web/, evals/, notes/); в исходниках совпадает с server/."""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
_HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.abspath(os.path.join(_HERE, ".."))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR) if FROZEN else APP_DIR
WEB_DIR = os.path.join(RES_DIR, "onec_agent", "web") if FROZEN else os.path.join(_HERE, "web")
EVALS_DIR = os.path.join(RES_DIR, "evals")

PRODUCT_NAME = os.environ.get("ONEC_AGENT_PRODUCT", "ИИ-консультант 1С")

HOST = os.environ.get("ONEC_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ONEC_AGENT_PORT", "8765"))

# Любой OpenAI-совместимый endpoint: LM Studio, Ollama, vLLM, облако.
LLM_BASE_URL = os.environ.get("ONEC_AGENT_LLM_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = os.environ.get("ONEC_AGENT_LLM_MODEL", "qwen/qwen3-30b-a3b-2507")
LLM_API_KEY = os.environ.get("ONEC_AGENT_LLM_KEY", "lm-studio")
LLM_TEMPERATURE = float(os.environ.get("ONEC_AGENT_LLM_TEMPERATURE", "0"))

MAX_AGENT_STEPS = int(os.environ.get("ONEC_AGENT_MAX_STEPS", "15"))
# С запасом: инструмент ждёт и решения пользователя в диалоге 1С.
TOOL_TIMEOUT_SEC = int(os.environ.get("ONEC_AGENT_TOOL_TIMEOUT", "900"))
# Длительные действия (закрытие месяца с перепроведением): на большой базе — десятки минут.
LONG_TOOL_TIMEOUT_SEC = int(os.environ.get("ONEC_AGENT_LONG_TOOL_TIMEOUT", "3600"))
# Инструменты без окна подтверждения (чтение) отвечают за секунды: если 1С молчит минуту — вкладка
# зависла или сессия устарела, ждать дальше бессмысленно.
READ_TOOL_TIMEOUT_SEC = int(os.environ.get("ONEC_AGENT_READ_TOOL_TIMEOUT", "60"))
# Сколько ждать очередной кусок ответа модели: холодный старт локальной модели — минуты, зависание — навсегда.
LLM_READ_TIMEOUT_SEC = int(os.environ.get("ONEC_AGENT_LLM_READ_TIMEOUT", "300"))
# Сколько символов результата инструмента отдаём модели (контекст локальной модели невелик).
TOOL_RESULT_LIMIT = int(os.environ.get("ONEC_AGENT_TOOL_RESULT_LIMIT", "8000"))

# Журнал вызовов инструментов (аргументы и сводки, без данных из базы).
AUDIT_LOG = os.environ.get("ONEC_AGENT_AUDIT_LOG", os.path.join(APP_DIR, "logs", "audit.jsonl"))

# Памятки по конфигурациям: notes/<Метаданные.Имя>.md подмешивается в системный промпт.
NOTES_DIR = os.environ.get("ONEC_AGENT_NOTES_DIR", os.path.join(RES_DIR, "notes"))

# Вложения: где хранить, лимит размера, сколько жить, чем распознавать скриншоты.
UPLOAD_DIR = os.environ.get("ONEC_AGENT_UPLOAD_DIR", os.path.join(APP_DIR, "data", "uploads"))
UPLOAD_MAX_BYTES = int(os.environ.get("ONEC_AGENT_UPLOAD_MAX_MB", "20")) * 1024 * 1024
UPLOAD_TTL_HOURS = int(os.environ.get("ONEC_AGENT_UPLOAD_TTL_HOURS", "72"))
VISION_BASE_URL = os.environ.get("ONEC_AGENT_VISION_URL", LLM_BASE_URL)
VISION_MODEL = os.environ.get("ONEC_AGENT_VISION_MODEL", "google/gemma-4-e4b")

# Токен доступа к серверу (см. auth.py): из переменной окружения или из файла, который создаётся сам.
TOKEN = os.environ.get("ONEC_AGENT_TOKEN", "")
TOKEN_FILE = os.environ.get("ONEC_AGENT_TOKEN_FILE", os.path.join(APP_DIR, "data", "token"))

# Профили модели по базам (выбор источника в окне настроек консультанта). Содержит ключи API — права 600.
MODELS_FILE = os.environ.get("ONEC_AGENT_MODELS_FILE", os.path.join(APP_DIR, "data", "models.json"))

# Журналы и вспомогательные программы — рядом с данными.
LOG_DIR = os.environ.get("ONEC_AGENT_LOG_DIR", os.path.join(APP_DIR, "logs"))

# Встроенная локальная модель (local_model.py): где лежат GGUF, чем запускать, на каком порту.
MODELS_DIR = os.environ.get("ONEC_AGENT_MODELS_DIR", os.path.join(APP_DIR, "data", "models"))
LOCAL_MODEL_SETTINGS = os.environ.get("ONEC_AGENT_LOCAL_MODEL", os.path.join(APP_DIR, "data", "local_model.json"))
LLAMA_SERVER = os.environ.get("ONEC_AGENT_LLAMA_SERVER", "")
LLAMA_PORT = int(os.environ.get("ONEC_AGENT_LLAMA_PORT", "8766"))

# Базы 1С, добавленные в приложении вручную, и итоги выгрузки исходников (bases.py).
BASES_FILE = os.environ.get("ONEC_AGENT_BASES", os.path.join(APP_DIR, "data", "bases.json"))
