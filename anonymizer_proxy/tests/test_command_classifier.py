"""
Функциональные тесты гибридного детектирования чат-команд прокси.

Проверяют исправление дефекта «команды перестали работать»:

1. Regex-правила: «перезагрузи прокси» (раньше не распознавалось — правила
   знали только «перезапусти»).
2. GLiNER-слой classify_command_intent (фейковый движок): порог, отрыв
   от второй метки, режим off.
3. resolve_chat_command на RequestHandler с фейками: все виды команд,
   перезапуск без обращения к GLiNER, работа при недоступности GLiNER.

Запуск: python anonymizer_proxy\\tests\\test_command_classifier.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from anonymizer_proxy.config import COMMAND_CLASSIFIER
from anonymizer_proxy.models.schemas import (
    ChatCompletionRequest,
    ChatMessage,
)
from anonymizer_proxy.proxy import command_classifier as cc
from anonymizer_proxy.proxy.command_classifier import (
    classify_command_intent,
    looks_like_command,
)
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.proxy.utils import (
    RESTART_INTENT_RE,
    COMMAND_TRIGGER_RE,
)

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK " if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


class FakeGliner:
    """Фейковый GlinerEngine: отдаёт готовые сущности, считает вызовы."""

    def __init__(self, ents=None, error=None):
        self.ents = ents or []
        self.error = error
        self.calls: list[str] = []

    async def predict_with_labels(self, text, labels, threshold=0.0,
                                  timeout=None):
        if self.error is not None:
            raise self.error
        self.calls.append(text)
        return self.ents


def label(intent: str) -> str:
    return cc.COMMAND_LABELS[intent]


def make_request(text: str, history: list[ChatMessage] | None = None,
                 anonymize: bool = True):
    messages = list(history or []) + [ChatMessage(role="user", content=text)]
    return ChatCompletionRequest(model="m", messages=messages,
                                 anonymize=anonymize)


def make_handler(engine) -> RequestHandler:
    return RequestHandler(
        ner_service=SimpleNamespace(engine=engine),
        mapping_store=SimpleNamespace(),
        openrouter_client=SimpleNamespace(backend="openrouter"),
    )


# ==================== 1. Regex-правила ====================

def test_regex_rules() -> None:
    print("1. Regex-правила")
    for phrase in ("перезагрузи прокси", "перезапусти прокси",
                   "рестартни сервер", "рестартани прокси",
                   "перезапуск прокси-сервера"):
        check(f"RESTART_INTENT_RE: «{phrase}»",
              bool(RESTART_INTENT_RE.search(phrase)))
    check("RESTART_INTENT_RE НЕ матчит «перезапусти тестовый сервер»",
          not RESTART_INTENT_RE.search("перезапусти тестовый сервер приложений"))
    check("RESTART_INTENT_RE НЕ матчит «перезагрузи страницу»",
          not RESTART_INTENT_RE.search("перезагрузи страницу"))

    check("COMMAND_TRIGGER_RE: обычный запрос мимо префильтра",
          not COMMAND_TRIGGER_RE.search("сравни два файла и составь отчёт"))
    check("COMMAND_TRIGGER_RE: «скрой все данные» проходит префильтр",
          bool(COMMAND_TRIGGER_RE.search("скрой все данные отчет.docx")))
    check("COMMAND_TRIGGER_RE: «раскрой все данные» проходит префильтр",
          bool(COMMAND_TRIGGER_RE.search("раскрой все данные")))


# ==================== 2. GLiNER-слой ====================

async def test_gliner_layer() -> None:
    print("2. GLiNER-слой classify_command_intent")
    saved = dict(COMMAND_CLASSIFIER)
    try:
        # GLiNER-слой выключен по умолчанию — включаем для теста
        COMMAND_CLASSIFIER["mode"] = "auto"
        # Порог пройден, отрыв есть
        engine = FakeGliner([
            {"label": label("deanon_files"), "score": 0.12, "text": "файлы"},
        ])
        intent = await classify_command_intent(engine, ["восстанови файлы"])
        check("порог+отрыв: deanon_files", intent == "deanon_files",
              f"получено {intent}")

        # Скор ниже порога
        engine = FakeGliner([
            {"label": label("deanon_files"), "score": 0.03, "text": "файлы"},
        ])
        intent = await classify_command_intent(engine, ["что-то про файлы"])
        check("скор ниже порога -> None", intent is None, f"получено {intent}")

        # Скоры близки — воздержаться
        engine = FakeGliner([
            {"label": label("backend_local"), "score": 0.10, "text": "локальную"},
            {"label": label("backend_cloud"), "score": 0.09, "text": "облако"},
        ])
        intent = await classify_command_intent(engine, ["переключи бэкенд"])
        check("близкие скоры -> None", intent is None, f"получено {intent}")

        # Пустой ответ движка
        intent = await classify_command_intent(FakeGliner([]), ["привет"])
        check("пустой ответ -> None", intent is None)

        # Режим off — движок даже не вызывается
        COMMAND_CLASSIFIER["mode"] = "off"
        engine = FakeGliner([
            {"label": label("backend_local"), "score": 0.9, "text": "локальную"},
        ])
        intent = await classify_command_intent(engine, ["работай локально"])
        check("mode=off -> None без вызова движка",
              intent is None and not engine.calls)
    finally:
        COMMAND_CLASSIFIER.clear()
        COMMAND_CLASSIFIER.update(saved)


# ==================== 3. resolve_chat_command ====================

async def test_resolve_chat_command() -> None:
    print("3. resolve_chat_command (RequestHandler с фейками)")
    saved = dict(COMMAND_CLASSIFIER)
    try:
        # GLiNER-слой выключен по умолчанию — включаем для теста
        COMMAND_CLASSIFIER["mode"] = "auto"
        # «перезагрузи прокси» — правила, GLiNER не вызывается
        engine = FakeGliner([])
        handler = make_handler(engine)
        cmd = await handler.resolve_chat_command(
            make_request("перезагрузи прокси"))
        check("«перезагрузи прокси» -> restart",
              cmd == {"command": "restart"}, f"получено {cmd}")
        check("GLiNER не вызывался", not engine.calls)

        # Свободная формулировка переключения бэкенда -> GLiNER
        engine = FakeGliner([
            {"label": label("backend_local"), "score": 0.12,
             "text": "локальной модели"},
        ])
        handler = make_handler(engine)
        cmd = await handler.resolve_chat_command(make_request(
            "давай дальше считаем по нашей локальной модели"))
        check("свободная формулировка -> backend local (GLiNER)",
              cmd == {"command": "backend", "backend": "local"},
              f"получено {cmd}")

        # Обычный запрос с триггер-словом: GLiNER пусто -> None
        handler = make_handler(FakeGliner([]))
        cmd = await handler.resolve_chat_command(make_request(
            "опиши архитектуру прокси-сервера в проекте"))
        check("обычный запрос (триггер есть) -> None",
              cmd is None, f"получено {cmd}")

        # Обычный запрос без триггера -> None, GLiNER не дёргается
        engine = FakeGliner([
            {"label": label("deanon_files"), "score": 0.9, "text": "файлы"},
        ])
        handler = make_handler(engine)
        cmd = await handler.resolve_chat_command(make_request(
            "сравни два файла и составь отчёт"))
        check("обычный запрос мимо префильтра -> None без вызова GLiNER",
              cmd is None and not engine.calls)

        # GLiNER недоступна — фоллбек на правила: полная де-анонимизация
        history = [ChatMessage(role="assistant", content=(
            "[ANONYMIZER]\nsession_id: testsession123\n"
            "[anonymizer:result:C:/x/КП.result.docx]\n"
            "[anonymizer:copy:C:/x/КП.anonymized.docx]\n"))]
        handler = make_handler(FakeGliner(error=RuntimeError("нет модели")))
        cmd = await handler.resolve_chat_command(make_request(
            "раскрой все данные", history))
        check("GLiNER упала -> фоллбек deanon_files",
              bool(cmd) and cmd.get("command") == "deanon_files",
              f"получено {cmd}")
        targets = (cmd or {}).get("targets") or []
        check("цель — файл результата из маркера",
              bool(targets)
              and targets[0].get("result_path") == "C:/x/КП.result.docx",
              f"получено {targets}")

        # Контентная команда при anonymize=false — явный opt-out клиента:
        # прокси не вмешивается в контент (контракт test_deanonymize_intercept)
        handler = make_handler(FakeGliner([]))
        cmd = await handler.resolve_chat_command(make_request(
            "раскрой все данные", history, anonymize=False))
        check("anonymize=false: контентная команда не перехватывается",
              cmd is None, f"получено {cmd}")

        # Но команда УПРАВЛЕНИЯ при anonymize=false работает всегда
        cmd = await handler.resolve_chat_command(make_request(
            "перезагрузи прокси", anonymize=False))
        check("anonymize=false: перезапуск всё равно перехватывается",
              cmd == {"command": "restart"}, f"получено {cmd}")
    finally:
        COMMAND_CLASSIFIER.clear()
        COMMAND_CLASSIFIER.update(saved)


if __name__ == "__main__":
    test_regex_rules()
    asyncio.run(test_gliner_layer())
    asyncio.run(test_resolve_chat_command())
    if FAILED:
        print(f"\nИТОГ: ПРОВАЛЕНО проверок: {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("\nИТОГ: все проверки пройдены")