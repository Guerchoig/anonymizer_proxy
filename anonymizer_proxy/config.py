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

# LM Studio (локальная NER-модель)
LM_STUDIO = {
    "base_url": os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
    "model": os.getenv("LM_STUDIO_MODEL", "qwen/qwen3.5-9b"),
    # Таймаут NER-запроса: большие документы обрабатываются локальной
    # моделью заметно дольше, чем короткие сообщения
    "timeout": float(os.getenv("NER_TIMEOUT_SECONDS", "600")),
    "temperature": 0.1,
    # Лимит токенов ответа NER: список сущностей большого документа
    # не помещается в 4096 токенов, хвост теряется
    "max_tokens": int(os.getenv("NER_MAX_TOKENS", "16384")),
    # Максимальный размер текста (символы), отдаваемый NER-модели за один
    # вызов. Задаётся по вместимости VRAM/контекста модели; оставляйте
    # запас на системный промпт и генерацию ответа.
    # Текст длиннее режется на чанки с перекрытием (chunk_overlap_chars).
    "max_input_chars": int(os.getenv("NER_MAX_INPUT_CHARS", "128000")),
    # Перекрытие соседних чанков (символы) при нарезке длинного текста:
    # защищает сущности, попавшие на границу чанков
    "chunk_overlap_chars": 500,
    # Число одновременно обрабатываемых NER-чанков (параллелизм). По умолчанию 1
    # (последовательно): конкурентные запросы к LM Studio (0.4.x) могут приводить
    # к зависанию модели («Channel Error» после обрыва) — не повышайте без нужды.
    "ner_parallel": int(os.getenv("NER_PARALLEL", "1")),
    # Кэш результата проверки доступности LM Studio (секунды)
    "availability_cache_seconds": 10.0,
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
    "POSITION": "Должности (ген. директор, гл. бухгалтер)",
    "DEPARTMENT": "Подразделения (отдел кадров, цех №5)",
    "ORG": "Названия компаний и юрлиц",
    "LOC": "Географические названия (города, адреса)",
    "PRODUCT": "Названия продуктов и систем",
    "PASSPORT": "Паспортные данные (серия, номер, кем выдан)",
    "PHONE": "Телефоны",
    "EMAIL": "Email адреса",
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