"""Настройки сервера. Всё переопределяется переменными окружения ONEC_AGENT_*."""
import os

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
# Инструменты без окна подтверждения (чтение) отвечают за секунды: если 1С молчит минуту — вкладка
# зависла или сессия устарела, ждать дальше бессмысленно.
READ_TOOL_TIMEOUT_SEC = int(os.environ.get("ONEC_AGENT_READ_TOOL_TIMEOUT", "60"))
# Сколько символов результата инструмента отдаём модели (контекст локальной модели невелик).
TOOL_RESULT_LIMIT = int(os.environ.get("ONEC_AGENT_TOOL_RESULT_LIMIT", "8000"))

# Журнал вызовов инструментов (аргументы и сводки, без данных из базы).
AUDIT_LOG = os.environ.get("ONEC_AGENT_AUDIT_LOG", os.path.join(os.path.dirname(__file__), "..", "logs", "audit.jsonl"))

# Памятки по конфигурациям: notes/<Метаданные.Имя>.md подмешивается в системный промпт.
NOTES_DIR = os.environ.get("ONEC_AGENT_NOTES_DIR", os.path.join(os.path.dirname(__file__), "..", "notes"))

# Вложения: где хранить, лимит размера, сколько жить, чем распознавать скриншоты.
UPLOAD_DIR = os.environ.get("ONEC_AGENT_UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))
UPLOAD_MAX_BYTES = int(os.environ.get("ONEC_AGENT_UPLOAD_MAX_MB", "20")) * 1024 * 1024
UPLOAD_TTL_HOURS = int(os.environ.get("ONEC_AGENT_UPLOAD_TTL_HOURS", "72"))
VISION_BASE_URL = os.environ.get("ONEC_AGENT_VISION_URL", LLM_BASE_URL)
VISION_MODEL = os.environ.get("ONEC_AGENT_VISION_MODEL", "google/gemma-4-e4b")

# Выгрузки конфигураций в файлы для code_read / code_search: {Метаданные.Имя: [{path, label}]}.
SOURCES_FILE = os.environ.get("ONEC_AGENT_SOURCES", os.path.join(os.path.dirname(__file__), "..", "sources.json"))

# База знаний для kb_search: инструкции, регламенты, решённые обращения (подкаталог tickets/).
KB_DIR = os.environ.get("ONEC_AGENT_KB_DIR", os.path.join(os.path.dirname(__file__), "..", "kb"))

# Токен доступа к серверу (см. auth.py): из переменной окружения или из файла, который создаётся сам.
TOKEN = os.environ.get("ONEC_AGENT_TOKEN", "")
TOKEN_FILE = os.environ.get("ONEC_AGENT_TOKEN_FILE", os.path.join(os.path.dirname(__file__), "..", "data", "token"))

# Профили модели по базам (выбор источника в окне настроек консультанта). Содержит ключи API — права 600.
MODELS_FILE = os.environ.get("ONEC_AGENT_MODELS_FILE", os.path.join(os.path.dirname(__file__), "..", "data", "models.json"))
