"""
Функциональные тесты извлечения содержимого анонимизированных копий
в manual-режиме.

Проверяют, что когда клиент (Cline) не смог прочитать бинарный файл и
прислал заглушку «Error fetching content» в <file_content path="...">:
1. Для анонимизированной копии (<name>.anonymized.<ext>) прокси сам читает
   локальный файл и подставляет извлечённый текст — облако получает
   анонимизированное содержимое.
2. Обычный (не анонимизированный) файл НЕ трогается — заглушка уходит в
   облако как есть (контракт manual).

Запуск: python anonymizer_proxy\\tests\\test_manual_anonymized_extract.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_manual import FakeNER, FakeStore, FakeOpenRouter

handlers_module.CURRENT_MODE = "manual"

CLOUD_RESPONSE = {
    "id": "c-1", "created": 1, "model": "m",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Резюме готово"},
        "finish_reason": "stop",
    }],
    "usage": {},
}


def make_handler():
    return RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(CLOUD_RESPONSE),
    )


def make_request(path: Path) -> ChatCompletionRequest:
    content = (
        "Составь резюме файла\n\n"
        f'<file_content path="{path}">\n'
        "Error fetching content: Cannot read binary file into context.\n"
        "</file_content>"
    )
    return ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[ChatMessage(role="user", content=content)],
    )


async def test_extracts_anonymized_copy():
    """Анонимизированная копия: текст извлекается и уходит в облако"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("Договор с [PERSON_1], подписант [ORG_1]")
    tmp.close()
    path = Path(tmp.name)
    try:
        handler = make_handler()
        await handler.handle_chat_completion(make_request(path))
        sent = handler.openrouter.captured["messages"][0]["content"]
        assert "Договор с [PERSON_1], подписант [ORG_1]" in sent, sent
        assert "Error fetching content" not in sent, sent
        print("TEST 1 OK: анонимизированная копия — текст извлечён")
    finally:
        path.unlink(missing_ok=True)


async def test_does_not_extract_plain_file():
    """Обычный файл: заглушка остаётся как есть (manual-контракт)"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("Секретный текст Ивана Петрова")
    tmp.close()
    path = Path(tmp.name)
    try:
        handler = make_handler()
        await handler.handle_chat_completion(make_request(path))
        sent = handler.openrouter.captured["messages"][0]["content"]
        assert "Error fetching content" in sent, sent
        assert "Ивана Петрова" not in sent, sent
        print("TEST 2 OK: обычный файл — заглушка не тронута")
    finally:
        path.unlink(missing_ok=True)


async def test_stream_extracts_anonymized_copy():
    """Стрим: перед отправкой в облако текст копии извлечён"""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("Договор с [PERSON_1]")
    tmp.close()
    path = Path(tmp.name)
    try:
        stream_chunks = [
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "Резюме"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        handler = RequestHandler(
            ner_service=FakeNER({}),
            mapping_store=FakeStore(),
            openrouter_client=FakeOpenRouter(None, stream_chunks),
        )
        request = make_request(path)
        request.stream = True
        out = []
        async for chunk in handler.stream_manual(request):
            out.append(chunk)
        sent = handler.openrouter.captured["messages"][0]["content"]
        assert "Договор с [PERSON_1]" in sent, sent
        assert any("[DONE]" in c for c in out), out
        print("TEST 3 OK: стрим — текст копии извлечён до отправки")
    finally:
        path.unlink(missing_ok=True)


async def main():
    await test_extracts_anonymized_copy()
    await test_does_not_extract_plain_file()
    await test_stream_extracts_anonymized_copy()
    print("\nALL ANONYMIZED EXTRACT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
