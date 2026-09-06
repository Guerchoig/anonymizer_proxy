"""
Anonymizer Proxy - Прокси-сервер для анонимизации запросов к облачным LLM

Архитектура:
    Cline → [Прокси localhost:8081] → OpenRouter → Qwen3.7-Max
                  ↓ ↑
            [GLiNER NER]
                  ↓ ↑
            [SQLite маппинги]

Режимы работы:
    - full: Анонимизация → Облако → Де-анонимизация
    - passthrough: явное управление анонимизацией командами в чате (основной)

Использование:
    python -m anonymizer_proxy.main
    # или:
    from anonymizer_proxy.main import main
    main()  # Запуск сервера
"""

from .config import Mode, CURRENT_MODE, PROXY_VERSION

__version__ = PROXY_VERSION
__author__ = "Anonymizer Proxy"
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