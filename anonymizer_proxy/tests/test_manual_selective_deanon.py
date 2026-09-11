"""
Функциональные тесты выборочной де-анонимизации в manual-режиме.

Проверяют, что когда в истории диалога есть маркер анонимизации
([ANONYMIZER] + session_id: ...), прокси де-анонимизирует ТОЛЬКО текстовое
содержимое ответа (message.content / delta.content), а аргументы tool_calls
остаются с плейсхолдерами (модель правит анонимизированную копию).

Запуск: python anonymizer_proxy\\tests\\test_manual_selective_deanon.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_manual import FakeNER, FakeStore, FakeOpenRouter

handlers_module.CURRENT_MODE = "manual"


def make_handler(store, response=None, stream_chunks=None):
    return RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(response, stream_chunks),
    )


def make_store(mappings=None):
    store = FakeStore()
    store.mappings = dict(mappings or {})
    return store


def history_with_anonymizer() -> list[ChatMessage]:
    """Диалог, в котором файл уже был анонимизирован (ответ перехвата)."""
    return [
        ChatMessage(role="user", content="Скрой все данные"),
        ChatMessage(
            role="assistant",
            content=(
                "[ANONYMIZER] Приложенные файлы анонимизированы локальной "
                "NER-моделью.\n\n"
                "session_id: sess-test\n\n"
                "Файлы:\n"
                "- C:/doc.docx → C:/doc.anonymized.docx\n"
                "  [anonymizer:done:C:/doc.docx]"
            ),
        ),
        ChatMessage(role="user", content="Теперь составь резюме"),
    ]


async def test_content_deanonymized_tool_calls_not():
    """Не-стрим: content де-анонимизируется, tool_calls — нет"""
    store = make_store({"[PERSON_1]": "Иван Петров", "[ORG_1]": "ООО Ромашка"})
    handler = make_handler(store, response={
        "id": "c-1", "created": 1, "model": "m",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Договор с [PERSON_1] из [ORG_1]",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "write_to_file",
                        "arguments": json.dumps({
                            "path": "C:/doc.anonymized.docx",
                            "content": "Подписант [PERSON_1]",
                        }),
                    },
                }],
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    })
    request = ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=history_with_anonymizer(),
    )
    resp, sid = await handler.handle_chat_completion(request)

    msg = resp.choices[0].message
    # content де-анонимизирован
    assert msg.content == "Договор с Иван Петров из ООО Ромашка", msg.content
    # tool_calls НЕ тронуты — плейсхолдеры остались
    args = json.loads(msg.tool_calls[0]["function"]["arguments"])
    assert args["content"] == "Подписант [PERSON_1]", args["content"]
    assert resp.anonymization_metadata["mode"] == "manual_deanonymized"
    assert resp.anonymization_metadata["sessions"] == ["sess-test"]
    print("TEST 1 OK: content де-анонимизирован, tool_calls с плейсхолдерами")


async def test_stream_content_deanonymized_tool_calls_not():
    """Стрим: delta.content де-анонимизируется, delta.tool_calls — нет"""
    store = make_store({"[PERSON_1]": "Иван Петров"})
    stream_chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Подписант [PERSON_1]"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1",
            "type": "function", "function": {"name": "write_to_file", "arguments": "{\"content\":\"[PERSON_1]\"}"}}]},
            "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    handler = make_handler(store, stream_chunks=stream_chunks)
    request = ChatCompletionRequest(
        model="m", anonymize=True, stream=True,
        messages=history_with_anonymizer(),
    )

    out = []
    async for chunk in handler.stream_manual(request):
        out.append(chunk)

    assert any("[DONE]" in c for c in out), out
    content_parts = []
    tool_args_parts = []
    for raw in out:
        if not raw.startswith("data: ") or "[DONE]" in raw:
            continue
        payload = json.loads(raw[len("data: "):])
        delta = payload["choices"][0]["delta"]
        if delta.get("content"):
            content_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            if tc.get("function", {}).get("arguments"):
                tool_args_parts.append(tc["function"]["arguments"])
    full_content = "".join(content_parts)
    full_args = "".join(tool_args_parts)
    assert full_content == "Подписант Иван Петров", full_content
    assert "[PERSON_1]" in full_args, full_args
    assert "Иван Петров" not in full_args, full_args
    print("TEST 2 OK: стрим — content де-анонимизирован, tool_calls с плейсхолдерами")


async def test_no_marker_manual_unchanged():
    """Без маркера анонимизации — обычный manual (без де-анонимизации)"""
    store = make_store({"[PERSON_1]": "Иван Петров"})
    handler = make_handler(store, response={
        "id": "c-1", "created": 1, "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Ответ про [PERSON_1]"},
            "finish_reason": "stop",
        }],
        "usage": {},
    })
    request = ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[ChatMessage(role="user", content="Составь резюме файла")],
    )
    resp, _ = await handler.handle_chat_completion(request)
    assert resp.choices[0].message.content == "Ответ про [PERSON_1]"
    assert resp.anonymization_metadata["mode"] == "manual"
    print("TEST 3 OK: без маркера — обычный manual, без де-анонимизации")


async def main():
    await test_content_deanonymized_tool_calls_not()
    await test_stream_content_deanonymized_tool_calls_not()
    await test_no_marker_manual_unchanged()
    print("\nALL SELECTIVE DEANON TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

