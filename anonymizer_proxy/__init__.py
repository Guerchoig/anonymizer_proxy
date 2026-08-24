"""
Anonymizer Proxy - Прокси-сервер для анонимизации запросов к облачным LLM

Архитектура:
    Cline → [Прокси localhost:8081] → OpenRouter → Qwen3.7-Max
                  ↓ ↑
            [LM Studio NER]
                  ↓ ↑
            [SQLite маппинги]

Режимы работы:
    - full: Анонимизация → Облако → Де-анонимизация
    - anonymize_only: Только анонимизация без отправки в облако

Использование:
    python -m anonymizer_proxy.main
    # или:
    from anonymizer_proxy.main import main
    main()  # Запуск сервера
"""

__version__ = "1.0.0"
__author__ = "Anonymizer Proxy"

from .config import Mode, CURRENT_MODE
from .anonymizer import NERService, FileParser, MappingStore, TextReplacer
from .proxy import OpenRouterClient, RequestHandler

__all__ = [
    "Mode",
    "CURRENT_MODE",
    "NERService",
    "FileParser",
    "MappingStore",
    "TextReplacer",
    "OpenRouterClient",
    "RequestHandler",
]