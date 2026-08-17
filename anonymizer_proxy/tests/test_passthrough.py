"""
Функциональные тесты passthrough-режима (anonymize=False).

Проверяют, что при anonymize=False:
1. NER/анонимизация не вызываются.
2. В облако уходят ОРИГИНАЛЬНЫЕ сообщения (без плейсхолдеров).
3. Ответ возвращается как есть (без де-анонимизации).
4. Метаданные указывают mode="passthrough".

Запуск: python anonymizer_proxy\\tests\\test_passthrough.py (из корня проекта)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import FakeStore, FakeOpenRouter


class CountingNER:
    """NER, который только считает вызовы (passthrough не должен его звать)"""

    def __init__(self):
        self.calls = 0

    async def extract_entities(self, text, use_llm=True):
        self.calls += 1
        return [], 0


def make_handler(ner, response=None, stream_chunks=None):
    return RequestHandler(
        ner_service=ner,
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(response, stream_chunks),
    )


async def test_passthrough_no_stream():
    ner = CountingNER()
    handler = make_handler(
        ner,
        response={
            "id": "c-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Ответ про [PERSON_1]"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    request = ChatCompletionRequest(
        model="m",
        anonymize=False,
        stream=False,
        messages=[ChatMessage(role="user", content="Проверь документ от Ивана Петрова")],
    )
    resp, sid = await handler.handle_chat_completion(request)

    # NER не вызывался
    assert ner.calls == 0, f"NER вызван {ner.calls} раз"
    # В облако ушли ОРИГИНАЛЬНЫЕ сообщения (не анонимизированные)
    sent = handler.openrouter.captured["messages"]
    assert sent[0]["content"] == "Проверь документ от Ивана Петрова", sent
    # Ответ НЕ де-анонимизирован
    assert resp.choices[0].message.content == "Ответ про [PERSON_1]"
    assert resp.anonymization_metadata["mode"] == "passthrough"
    print("TEST 1 OK: passthrough non-stream — без NER/анонимизации/де-анонимизации")


async def test_passthrough_stream():
    ner = CountingNER()
    stream_chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Привет"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    handler = make_handler(ner, stream_chunks=stream_chunks)
    request = ChatCompletionRequest(
        model="m",
        anonymize=False,
        stream=True,
        messages=[ChatMessage(role="user", content="Ивана Петрова")],
    )

    out = []
    async for chunk in handler.stream_passthrough(request):
        out.append(chunk)

    assert ner.calls == 0, f"NER вызван {ner.calls} раз"
    sent = handler.openrouter.captured["messages"]
    assert sent[0]["content"] == "Ивана Петрова", sent
    assert any("[DONE]" in c for c in out), out
    print("TEST 2 OK: passthrough stream — без NER/анонимизации")


async def main():
    await test_passthrough_no_stream()
    await test_passthrough_stream()
    print("\nALL PASSTHROUGH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
