"""
Функциональные тесты авто-де-анонимизации файлов по естественной команде
в passthrough-режиме («деанонимизируй упомянутые файлы»).

Проверяют, что когда в истории диалога есть маркер анонимизации
([anonymizer:copy:<путь>] + session_id), а пользователь просит
де-анонимизировать файлы:
1. Прокси де-анонимизирует файл (плейсхолдеры → реальные значения),
   облако НЕ вызывается.
2. Без команды де-анонимизации — обычный passthrough.
3. anonymize=false отключает перехват.

Запуск: python anonymizer_proxy\\tests\\test_deanonymize_intercept.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import FakeNER, FakeStore, FakeOpenRouter

handlers_module.CURRENT_MODE = "passthrough"

CLOUD_RESPONSE = {
    "id": "c-1", "created": 1, "model": "m",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {},
}


def make_handler(mappings=None):
    store = FakeStore()
    store.mappings = dict(mappings or {"[PERSON_1]": "Иван Петров"})
    return RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(CLOUD_RESPONSE),
    )


def make_history(copy_path: Path, command: str, anonymize=True) -> ChatCompletionRequest:
    messages = [
        ChatMessage(role="user", content="Анонимизируй приложенный файл"),
        ChatMessage(role="assistant", content=(
            "[ANONYMIZER] Приложенные файлы анонимизированы локальной "
            "NER-моделью.\n\n"
            "session_id: sess-test\n\n"
            "Файлы:\n"
            f"- C:/orig.docx → {copy_path} (сущностей: 1)\n"
            "  [anonymizer:done:C:/orig.docx]\n"
            f"  [anonymizer:copy:{copy_path}]"
        )),
        ChatMessage(role="user", content=command),
    ]
    return ChatCompletionRequest(
        model="m", anonymize=anonymize, stream=False, messages=messages,
    )


async def test_deanonymize_intercept():
    """Команда «деанонимизируй файлы» → файл де-анонимизирован, облако не вызвано"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("Подписант: [PERSON_1]")
    tmp.close()
    path = Path(tmp.name)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(path, "Деанонимизируй упомянутые файлы")
        )
        assert not handler.openrouter.captured, "облако вызвано"
        assert path.read_text(encoding="utf-8") == "Подписант: Иван Петров"
        assert resp.anonymization_metadata["mode"] == "files_deanonymization"
        assert "Иван Петров" not in resp.choices[0].message.content  # ответ-подтверждение без PII
        print("TEST 1 OK: деанонимизируй файлы — файл восстановлен, облако не вызвано")
    finally:
        path.unlink(missing_ok=True)


async def test_no_command_passthrough():
    """Без команды де-анонимизации — обычный passthrough в облако"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("[PERSON_1]")
    tmp.close()
    path = Path(tmp.name)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(path, "Составь резюме")
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough_deanonymized"
        assert path.read_text(encoding="utf-8") == "[PERSON_1]"  # файл не тронут
        print("TEST 2 OK: без команды — passthrough, файл не де-анонимизирован")
    finally:
        path.unlink(missing_ok=True)


async def test_anonymize_false_disables():
    """anonymize=false отключает перехват де-анонимизации"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("[PERSON_1]")
    tmp.close()
    path = Path(tmp.name)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(path, "Деанонимизируй файлы", anonymize=False)
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough"
        assert path.read_text(encoding="utf-8") == "[PERSON_1]"
        print("TEST 3 OK: anonymize=false — перехват де-анонимизации отключён")
    finally:
        path.unlink(missing_ok=True)


async def main():
    await test_deanonymize_intercept()
    await test_no_command_passthrough()
    await test_anonymize_false_disables()
    print("\nALL DEANONYMIZE INTERCEPT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
