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

# OpenRouter (облачная модель)
OPENROUTER = {
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": os.getenv("OPENROUTER_API_KEY", ""),
    "model": os.getenv("OPENROUTER_MODEL", "qwen/qwen-3.7-max"),
    "timeout": 120.0,
}

logger.debug("OPENROUTER_MODEL из .env: %s", OPENROUTER["model"])
logger.debug("OPENROUTER_PROXY: %s", os.getenv("OPENROUTER_PROXY", "не задан"))

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
    ANONYMIZE_ONLY = "anonymize_only"  # Только анонимизация без отправки в облако
    PASSTHROUGH = "passthrough"        # Passthrough (без анонимизации): явная схема управления
    
CURRENT_MODE = os.getenv("ANONYMIZER_MODE", Mode.PASSTHROUGH)

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

# TTL для маппингов (в секундах)
MAPPING_TTL_SECONDS = 24 * 60 * 60  # 24 часа

# Маркеры выделения канонического анонимизированного результата в ответе.
# Результат между RESULT_BEGIN и RESULT_END идентичен содержимому файла
# data/anonymized_files/<session_id>/anonymized_request_*.md
RESULT_BEGIN = "<<<ANONYMIZED_RESULT>>>"
RESULT_END = "<<<END_ANONYMIZED_RESULT>>>"