"""
Конфигурация прокси-сервера анонимизации
"""
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Базовые пути: корень проекта (на уровень выше пакета anonymizer_proxy)
BASE_DIR = Path(__file__).resolve().parent.parent

# Загружаем переменные окружения из .env файла
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
ANONYMIZED_FILES_DIR = DATA_DIR / "anonymized_files"
MAPPINGS_DIR = DATA_DIR / "mappings"
DB_PATH = DATA_DIR / "anonymizer.db"

def ensure_directories() -> None:
    """Создать рабочие каталоги (вызывается явно, а не при импорте)."""
    for dir_path in [DATA_DIR, LOGS_DIR, ANONYMIZED_FILES_DIR, MAPPINGS_DIR]:
        dir_path.mkdir(parents=True, exist_ok=True)


# ==================== Логирование ====================

def setup_logging() -> logging.Logger:
    """Настроить логирование пакета (вместо print)"""
    logger = logging.getLogger("anonymizer_proxy")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


logger = setup_logging()

# NER-движок (GLiNER) — локальная анонимизация
NER_ENGINE = {
    "model": os.getenv("NER_MODEL", "knowledgator/gliner-pii-large-v1.0"),
    "threshold": float(os.getenv("NER_THRESHOLD", "0.3")),
    # Бэкенд инференса: auto|onnx — ONNX Runtime с автовыбором провайдера
    # (CUDA → DirectML → CPU); torch — PyTorch (аварийный фоллбек,
    # если ONNX не загрузился). При деградации в лог уходит WARNING.
    "backend": os.getenv("NER_BACKEND", "auto").strip().lower(),
    # Устройство для PyTorch-фоллбэка: cpu | directml
    "device": os.getenv("NER_DEVICE", "cpu"),
    # Максимальный размер текста (символы), отдаваемый модели за один
    # вызов. Окно модели — 768 токенов (~2.4 симв./токен для русского):
    # 1500 символов гарантируют, что чанк не выходит за окно и хвост
    # не усекается молча (при 2000 сущности в конце чанка терялись).
    "max_input_chars": int(os.getenv("NER_MAX_INPUT_CHARS", "1500")),
    # Перекрытие соседних чанков (символы) при нарезке длинного текста:
    # защищает сущности, попавшие на границу чанков
    "chunk_overlap_chars": int(os.getenv("NER_CHUNK_OVERLAP_CHARS", "200")),
    # Таймаут NER-вызова (страховочный, применяется в GlinerEngine.predict)
    "timeout": float(os.getenv("NER_TIMEOUT_SECONDS", "300")),
    # Второй NER-контур — Natasha/Slovnet: русский NER (PER/ORG/LOC) +
    # yargy-извлекатель ФИО. Отключить: NER_NATASHA=0
    "natasha": os.getenv("NER_NATASHA", "1").strip().lower() in ("1", "true", "yes", "on"),
    # Каталог для весов Natasha/Slovnet (скачиваются при первом запуске)
    "models_dir": str(DATA_DIR / "models"),
}

# ==================== Облачные LLM-провайдеры (OpenAI-совместимые) ====================
# Все провайдеры работают по единому OpenAI-совместимому формату
# (POST {base_url}/chat/completions, SSE-стриминг, /models) и различаются
# только base_url, ключом, схемой авторизации и именами моделей.
#
# CLOUD_PROVIDER — СТАРТОВЫЙ облачный провайдер: действует сразу после
# установки (по умолчанию openrouter — существующие .env продолжают работать
# без изменений) и после сброса runtime-состояния. Далее пользователь может
# явно выбрать любого провайдера из реестра (форма /env-editor,
# POST /api/backend) — он становится ДЕЙСТВУЮЩИМ и запоминается в
# data/runtime_state.json (см. RUNTIME ниже).
CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER", "openrouter").strip().lower()

# VPN-прокси (Happ) нужен ТОЛЬКО для OpenRouter (доступ к openrouter.ai из РФ).
# Российские провайдеры (gptunnel.ru, bothub.chat, api.aitunnel.ru,
# proxy.gen-api.ru) доступны напрямую и по умолчанию
# ХОДЯТ МИМО VPN: proxy=None. При необходимости прокси для конкретного
# провайдера включается отдельной переменной <NAME>_PROXY (по умолчанию не задана).
_OPENROUTER_PROXY = os.getenv("OPENROUTER_PROXY", None)


def _cloud_provider(
    name: str, base_url: str, model_env: str, model_default: str, *,
    auth_scheme: str = "bearer", proxy: str | None = None,
    browser_ua: bool = False, extra_headers: dict | None = None,
    timeout: float = 120.0, keys_url: str = "",
) -> dict:
    """Запись реестра облачных провайдеров с env-переопределениями.

    Для каждого провайдера <NAME> читаются из .env:
      <NAME>_BASE_URL, <NAME>_API_KEY, <NAME>_MODEL, <NAME>_PROXY, <NAME>_TIMEOUT,
    а модель — из переменной model_env (для openrouter это OPENROUTER_MODEL
    для обратной совместимости со старыми .env).
    """
    env_prefix = name.upper()
    if name == "openrouter":
        key_env, url_env = "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"
    else:
        key_env, url_env = f"{env_prefix}_API_KEY", f"{env_prefix}_BASE_URL"
    return {
        "name": name,
        "base_url": os.getenv(url_env, base_url).rstrip("/"),
        "api_key": os.getenv(key_env, ""),
        "model": os.getenv(model_env, model_default),
        # bearer — "Authorization: Bearer <key>"; plain — ключ без префикса
        "auth_scheme": auth_scheme,
        # None = прямой доступ (без VPN); переопределяется <NAME>_PROXY
        "proxy": os.getenv(f"{env_prefix}_PROXY", "") or proxy,
        # Браузерный User-Agent (для провайдеров с WAF, напр. OpenRouter)
        "browser_ua": browser_ua,
        "extra_headers": extra_headers or {},
        "timeout": float(os.getenv(f"{env_prefix}_TIMEOUT", str(timeout))),
        # Где взять ключ (для подсказок в форме/баннере/selftest)
        "keys_url": keys_url,
    }


CLOUD_PROVIDERS: dict[str, dict] = {
    "openrouter": _cloud_provider(
        "openrouter", "https://openrouter.ai/api/v1",
        "OPENROUTER_MODEL", "qwen/qwen-3.7-max",
        proxy=_OPENROUTER_PROXY, browser_ua=True,
        extra_headers={
            "HTTP-Referer": "http://localhost:8081",
            "X-Title": "Anonymizer Proxy",
        },
        keys_url="https://openrouter.ai/keys",
    ),
    # GPTunneL: по документации ключ передаётся в Authorization БЕЗ Bearer
    "gptunnel": _cloud_provider(
        "gptunnel", "https://gptunnel.ru/v1", "GPTUNNEL_MODEL", "gpt-4o",
        auth_scheme="plain",
        keys_url="https://gptunnel.ru/profile",
    ),
    # BotHub: нестандартный путь base URL (api/v2/openai/v1)
    "bothub": _cloud_provider(
        "bothub", "https://bothub.chat/api/v2/openai/v1",
        "BOTHUB_MODEL", "gpt-4o",
        keys_url="https://bothub.chat",
    ),
    # AITUNNEL: модели в формате провайдер/модель (как в OpenRouter)
    "aitunnel": _cloud_provider(
        "aitunnel", "https://api.aitunnel.ru/v1",
        "AITUNNEL_MODEL", "openai/gpt-4o",
        keys_url="https://aitunnel.ru",
    ),
    # GenAPI: OpenAI-совместимый endpoint для IDE/плагинов
    "genapi": _cloud_provider(
        "genapi", "https://proxy.gen-api.ru/v1", "GENAPI_MODEL", "gpt-5-4",
        keys_url="https://gen-api.ru",
    ),
    # Свободно конфигурируемый OpenAI-совместимый endpoint
    "custom": _cloud_provider(
        "custom", "", "CUSTOM_MODEL", "",
    ),
}

if CLOUD_PROVIDER not in CLOUD_PROVIDERS:
    logger.warning(
        "Неизвестный CLOUD_PROVIDER=%r (доступно: %s) — использую openrouter",
        CLOUD_PROVIDER, ", ".join(CLOUD_PROVIDERS))
    CLOUD_PROVIDER = "openrouter"

# Обратная совместимость: прежний словарь OPENROUTER — запись реестра
OPENROUTER = CLOUD_PROVIDERS["openrouter"]

logger.debug(
    "CLOUD_PROVIDER (дефолтный облачный провайдер): %s", CLOUD_PROVIDER)
logger.debug("OPENROUTER_MODEL из .env: %s", OPENROUTER["model"])
logger.debug(
    "OPENROUTER_PROXY: %s",
    _OPENROUTER_PROXY if _OPENROUTER_PROXY else "не задан")

# Прокси-сервер
# ВАЖНО: по умолчанию слушаем только localhost.
# 0.0.0.0 допустимо только за файрволом/туннелем и с включённым PROXY_API_TOKEN.
PROXY = {
    "host": os.getenv("PROXY_HOST", "127.0.0.1"),
    "port": int(os.getenv("PROXY_PORT", "8081")),
    # Токен для административных /api/* эндпоинтов.
    # Если не задан и хост не localhost — сервер не запустится.
    "api_token": os.getenv("PROXY_API_TOKEN", ""),
}

# Логирование: консоль (StreamHandler в setup_logging) + таблица logs в SQLite.
# Файловый экспорт логов — через /api/logs/export (пишет в LOGS_DIR).

# Хранение анонимизированных файлов
STORAGE = {
    "save_anonymized_files": True,  # Сохранять анонимизированные версии
    "save_original_files": True,    # Сохранять оригиналы (для сравнения)
    "retention_days": 30,           # Хранить файлы N дней
}

# Режимы работы
class Mode:
    FULL = "full"                      # Полная обработка: анонимизация → облако → де-анонимизация
    MANUAL = "manual"                  # Manual (ручное управление): анонимизация только по командам в чате

# Значение ANONYMIZER_MODE нормализуется: допустимы full и manual.
# Устаревшие значения не падают (прокси не падает на чужих .env):
# - anonymize_only удалён (v1.10.0);
# - passthrough переименован в manual (v1.12.0) — берётся manual с
#   предупреждением в логе.
_CURRENT_MODE_ENV = os.getenv("ANONYMIZER_MODE", Mode.MANUAL)
if _CURRENT_MODE_ENV == "passthrough":
    logger.warning(
        "ANONYMIZER_MODE=passthrough устарело (режим переименован в manual) — "
        "обновите .env; используется manual")
    _CURRENT_MODE_ENV = Mode.MANUAL
if _CURRENT_MODE_ENV in (Mode.FULL, Mode.MANUAL):
    CURRENT_MODE = _CURRENT_MODE_ENV
else:
    logger.warning(
        "ANONYMIZER_MODE=%s не поддерживается — "
        "используется manual", _CURRENT_MODE_ENV)
    CURRENT_MODE = Mode.MANUAL

# ==================== Локальная LLM (llama.cpp / llama-server) ====================
# llama-server поднимает OpenAI-совместимый HTTP-сервер (родной дефолт
# llama.cpp — порт 8080). Локальный бэкенд используется для сценариев,
# где анонимизация мешает работе (например, модель должна считать
# конфиденциальные суммы в ячейках): данные не покидают машину.
# Управление самим сервером (запуск/остановка/проверка) — модуль
# anonymizer_proxy.llm_server, конфигурация — секция LLM_SERVER ниже.

def _llm_server_base_url() -> str:
    """Base URL локального бэкенда: из LOCAL_LLM_BASE_URL, если задан явно,
    иначе конструируется из LLM_SERVER_HOST/PORT (единая точка правды для
    хоста и порта llama-server)."""
    explicit = os.getenv("LOCAL_LLM_BASE_URL", "").strip()
    if explicit:
        return explicit
    host = os.getenv("LLM_SERVER_HOST", "127.0.0.1")
    port = os.getenv("LLM_SERVER_PORT", "8080")
    return f"http://{host}:{port}/v1"


def _llm_server_api_key() -> str:
    """Ключ авторизации на llama-server: LOCAL_LLM_API_KEY имеет приоритет,
    иначе общий ключ сервера LLM_SERVER_API_KEY, иначе нейтральное значение
    (llama-server без --api-key заголовок Authorization игнорирует)."""
    return (os.getenv("LOCAL_LLM_API_KEY", "").strip()
            or os.getenv("LLM_SERVER_API_KEY", "").strip()
            or "llama-server")


LOCAL_LLM = {
    "base_url": _llm_server_base_url(),
    "api_key": _llm_server_api_key(),
    # Имя модели (алиас llama-server, например qwen3.5-9b-instruct).
    # Запросы с этим именем модели (или с префиксом "local/") роутер
    # направляет локально независимо от выбранного бэкенда. Если пусто —
    # берётся первая (единственная) модель llama-server через /v1/models.
    "model": os.getenv("LOCAL_LLM_MODEL", ""),
    # Локальная thinking-модель отвечает медленнее — таймаут больше.
    # При LLM_SERVER_PARALLEL=1 запрос может ждать в очереди за длинным
    # файловым прогоном другого приложения — таймаут должен покрывать и это.
    "timeout": float(os.getenv("LOCAL_LLM_TIMEOUT", "600")),
    # max_tokens клиента НЕ пересылается в llama-server: у локальной модели
    # нет бюджета выходных токенов (генерация бесплатна). Thinking-вывод
    # модели (reasoning_content и блоки размышлений) передаётся без вырезания.
}

# ==================== Управление llama-server (запуск/остановка) ====================
# llama-server — общий сервис машины: одна GGUF-модель в памяти, к нему
# ходят и прокси, и другие приложения (RAG и т.п.). Менеджер llm_server.py
# при старте прокси проверяет, нет ли уже живого инстанса (и в каком режиме
# он запущен), и переиспользует его вместо запуска второго.
LLM_SERVER = {
    # Путь к бинарю llama-server(.exe). Пусто — автопоиск: PATH,
    # tools/llama.cpp/ (Windows-пре-билд), brew --prefix (macOS).
    "bin": os.getenv("LLM_SERVER_BIN", ""),
    # Путь к GGUF-файлу модели
    # (например, data/models/llm/qwen3.5-9b-instruct-Q4_K_M.gguf)
    "model": os.getenv("LLM_SERVER_MODEL", ""),
    "host": os.getenv("LLM_SERVER_HOST", "127.0.0.1"),
    # Родной дефолт llama.cpp; менеджер всегда передаёт --port явно
    "port": int(os.getenv("LLM_SERVER_PORT", "8080")),
    # Слоты llama-server: 1 = запросы выполняются строго по очереди
    # (очередь бесплатна по памяти). Запрос всегда получает ровно
    # ctx_per_slot токенов контекста.
    "parallel": int(os.getenv("LLM_SERVER_PARALLEL", "1")),
    # Контекст ОДНОГО запроса (файл через прокси / RAG-поиск): 32K.
    # Общий --ctx-size = parallel × ctx_per_slot (вычисляет менеджер).
    "ctx_per_slot": int(os.getenv("LLM_SERVER_CTX_PER_SLOT", "32768")),
    # Ключ llama-server (--api-key); пусто — без авторизации (localhost)
    "api_key": os.getenv("LLM_SERVER_API_KEY", ""),
    # Прокси при старте сам проверяет/запускает llama-server (неблокирующе)
    "autostart": os.getenv("LLM_SERVER_AUTOSTART", "1").strip().lower() in ("1", "true", "yes", "on"),
    # Дополнительные флаги командной строки llama-server (GPU-слои,
    # квантование KV-кэша и т.п.)
    "extra_args": os.getenv("LLM_SERVER_EXTRA_ARGS", ""),
    # Сколько секунд ждать готовности /health при старте (загрузка GGUF
    # в память и выделение KV-кэша могут занимать десятки секунд)
    "start_timeout": float(os.getenv("LLM_SERVER_START_TIMEOUT", "300")),
}

# ==================== Детектирование чат-команд ====================
# Гибридная схема: дешёвый regex-префильтр (отсекает обычные промты) →
# детерминированные правила для точных формулировок → GLiNER zero-shot для
# свободных формулировок «безопасных» команд. GLiNER уже загружен в процесс
# прокси (NER-контур): детекция команд офлайн и не зависит от llama-server.
# Перезапуск прокси — ТОЛЬКО по правилам (деструктивная команда; GLiNER
# путает «перезапусти прокси» с «перезапусти тестовый сервер» — проверено).
COMMAND_CLASSIFIER = {
    # off (по умолчанию) — детекция команд ТОЛЬКО детерминированными
    #   regex-правилами: zero-shot классификаторы (GLiNER, NLI mDeBERTa) в
    #   роли детектора команд нестабильны — скоры не калиброваны и сильно
    #   зависят от формулировок (проверено живыми тестами на mDeBERTa);
    # auto — включить GLiNER-слой свободных формулировок (см. ниже)
    "mode": os.getenv("COMMAND_CLASSIFIER", "off").strip().lower(),
    # Порог лучшего скора для признания командой (GLiNER-слой: позитивы
    # дают 0.05–0.23)
    "threshold": float(os.getenv("COMMAND_CLASSIFIER_THRESHOLD", "0.05")),
    # Требуемый отрыв от второй по скору метки (при близких скорах —
    # воздержаться и пойти обычным порядком)
    "margin": float(os.getenv("COMMAND_CLASSIFIER_MARGIN", "1.3")),
    # Ограничение текста, отдаваемого классификатору (сообщения короткие)
    "max_chars": int(os.getenv("COMMAND_CLASSIFIER_MAX_CHARS", "2000")),
    # Таймаут инференса классификатора (сек) — страховка от зависания
    "timeout": float(os.getenv("COMMAND_CLASSIFIER_TIMEOUT", "30")),
}

# ==================== Рантайм-состояние (без перезапуска) ====================
# Действующий LLM-бэкенд переключается на лету (форма /env-editor,
# POST /api/backend, заголовок X-LLM-Backend). Не берётся из .env, чтобы
# не требовать перезапуска; выбор сохраняется в data/runtime_state.json.
#   backend — куда уходят запросы сейчас: "local" или провайдер реестра;
#   cloud   — ДЕЙСТВУЮЩИЙ облачный провайдер: последний явно выбранный.
#             Не затирается переходом на local — команда «работай через
#             облако» возвращает именно к нему. Пока пользователь не
#             переключался ни разу, равен стартовому CLOUD_PROVIDER.
RUNTIME = {"backend": CLOUD_PROVIDER, "cloud": CLOUD_PROVIDER}
RUNTIME_STATE_PATH = DATA_DIR / "runtime_state.json"


def load_runtime_state() -> None:
    """Восстановить рантайм-состояние с диска (с миграцией старого формата)."""
    import json
    try:
        if RUNTIME_STATE_PATH.is_file():
            state = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
            backend = state.get("backend")
            # local + любой облачный провайдер из реестра
            if backend == "local" or backend in CLOUD_PROVIDERS:
                RUNTIME["backend"] = backend
            # Действующий облачный провайдер. Миграция старого файла без
            # поля "cloud": облачный backend становится действующим; для
            # local информации нет — сбрасываем на стартовый CLOUD_PROVIDER.
            cloud = state.get("cloud")
            if cloud in CLOUD_PROVIDERS:
                RUNTIME["cloud"] = cloud
            elif backend in CLOUD_PROVIDERS:
                RUNTIME["cloud"] = backend
            else:
                RUNTIME["cloud"] = CLOUD_PROVIDER
    except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять старт
        logger.warning("Не удалось прочитать runtime_state.json: %s", exc)


def acting_cloud_provider() -> str:
    """ДЕЙСТВУЮЩИЙ облачный провайдер: последний явно выбранный; до первого
    переключения — стартовый CLOUD_PROVIDER (после установки — openrouter).
    Единая точка правды для «работай через облако», `cloud/…` и подсказок
    о ключе действующего провайдера."""
    return RUNTIME.get("cloud") or CLOUD_PROVIDER


def save_runtime_state() -> None:
    """Сохранить рантайм-состояние (активный бэкенд) на диск."""
    import json
    try:
        RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE_PATH.write_text(
            json.dumps(RUNTIME, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить runtime_state.json: %s", exc)


# Версия прокси — единый источник (выводится в /health, /api/status,
# FastAPI-приложении и баннере при старте). Увеличивайте при изменениях
# кода: Python не перезагружает код в работающем сервере, и по /health
# можно понять, выполняет ли процесс актуальную версию.
PROXY_VERSION = "1.17.0"

# Категории PII для детекции
PII_CATEGORIES = {
    "PERSON": "Имена и фамилии сотрудников",
    "SURNAME": "Фамилии (в том числе с инициалами, без имени)",
    "POSITION": "Должности (ген. директор, гл. бухгалтер)",
    "DEPARTMENT": "Подразделения (отдел кадров, цех №5)",
    "ORG": "Названия компаний и юрлиц",
    "LOC": "Географические названия (города, адреса)",
    "PRODUCT": "Названия продуктов и систем",
    "PASSPORT": "Паспортные данные (серия, номер, кем выдан)",
    "PHONE": "Телефоны",
    "EMAIL": "Email адреса",
    "WEB": "Адреса веб-сайтов (URL, домены)",
    "INN": "ИНН/ОГРН/КПП",
    "MONEY": "Суммы денег",
}

# Типы плейсхолдеров, допустимые в командах («Раскрой эти данные…»,
# замены при «Скрой эти данные…»): PII_CATEGORIES + MISC —
# произвольные строки (номера, даты и т.п.), заменяемые вручную командой
# точечной анонимизации. MISC сознательно НЕ добавлен в метки NER
# (PII_CATEGORIES), чтобы не менять поведение GLiNER.
PLACEHOLDER_TYPES = {
    **PII_CATEGORIES,
    "MISC": "Произвольные строки (номера, даты, суммы — ручная замена)",
}

# TTL для маппингов (в секундах)
MAPPING_TTL_SECONDS = 24 * 60 * 60  # 24 часа
