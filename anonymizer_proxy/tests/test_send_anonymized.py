"""
Функциональные тесты эндпоинта /api/send (отправка анонимизированного промпта).

Проверяют:
1. Не-стриминг: content уходит единым user-сообщением, ответ де-анонимизируется.
2. Стриминг: ответ де-анонимизируется в потоке.

Запуск: python anonymizer_proxy\\tests\\test_send_anonymized.py (из корня проекта)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import SendAnonymizedRequest
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import FakeNER, FakeStore, FakeOpenRouter


async def test_send_anonymized_no_stream():
    store = FakeStore()
    handler = RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(
            response={
                "id": "c", "created": 1, "model": "m",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Ответ про [PERSON_1]"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ),
    )
    await store.add_mapping("s1", "Ивана Петрова", "PERSON")

    request = SendAnonymizedRequest(session_id="s1", content="Проверь [PERSON_1]", stream=False)
    resp, sid = await handler.handle_send_anonymized(request)

    sent = handler.openrouter.captured["messages"]
    assert sent == [{"role": "user", "content": "Проверь [PERSON_1]"}], sent
    assert resp.choices[0].message.content == "Ответ про Ивана Петрова"
    assert resp.anonymization_metadata["mode"] == "send"
    assert sid == "s1"
    print("TEST 1 OK: /api/send non-stream — отправка + де-анонимизация")


async def test_send_anonymized_stream():
    store = FakeStore()
    await store.add_mapping("s1", "Ивана Петрова", "PERSON")
    stream_chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Ответ [PERSON_1]"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    handler = RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(stream_chunks=stream_chunks),
    )
    request = SendAnonymizedRequest(session_id="s1", content="Проверь [PERSON_1]", stream=True)

    out = []
    async for chunk in handler.stream_send_anonymized(request):
        out.append(chunk)

    assert any("[DONE]" in c for c in out), out
    joined = "".join(out)
    assert "Ответ Ивана Петрова" in joined, joined
    print("TEST 2 OK: /api/send stream — отправка + де-анонимизация")


async def main():
    await test_send_anonymized_no_stream()
    await test_send_anonymized_stream()
    print("\nALL SEND ANONYMIZED TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
