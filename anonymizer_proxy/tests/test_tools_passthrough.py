"""
Функциональные тесты проброса tool-calling (tools/tool_calls) через прокси.

Проверяют исправление дефекта, из-за которого облачная модель не получала
определения инструментов агента (например, Cline):
1. Схемы сохраняют tools/tool_choice и поля tool_calls/tool_call_id.
2. Анонимизация сообщений сохраняет структуру tool_calls и анонимизирует
   аргументы вызовов.
3. Не-стриминг: tools пробрасываются в OpenRouter, tool_calls ответа
   де-анонимизируются.
4. Стриминг: аргументы tool_calls де-анонимизируются с буферизацией
   разорванных плейсхолдеров.

Запуск: python anonymizer_proxy\\tests\\test_tools_passthrough.py (из корня проекта)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage, Entity
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler

# Тесты проверяют полный цикл анонимизации — не зависеть от ANONYMIZER_MODE в .env
handlers_module.CURRENT_MODE = "full"


# ==================== Фейки (без LM Studio и сети) ====================

class FakeNER:
    """NER, который находит заданные PII-строки по точному совпадению"""

    def __init__(self, pii: dict):
        self.pii = pii  # {значение: тип}

    async def extract_entities(self, text: str, use_llm: bool = True):
        entities = []
        for value, etype in self.pii.items():
            start = 0
            while True:
                idx = text.find(value, start)
                if idx == -1:
                    break
                entities.append(Entity(
                    text=value, type=etype,
                    start=idx, end=idx + len(value)
                ))
                start = idx + len(value)
        entities.sort(key=lambda e: e.start)
        return entities, 0

    async def extract_entities_detailed(self, text: str, use_llm: bool = True):
        entities, dt = await self.extract_entities(text, use_llm)
        return entities, dt, False


class FakeStore:
    def __init__(self):
        self.counters = {}
        self.mappings = {}  # token -> original
        self.file_sessions = {}  # file_path -> session_id

    async def register_file_session(self, file_path, session_id):
        self.file_sessions[str(file_path)] = session_id

    async def get_latest_session_for_file(self, file_path):
        return self.file_sessions.get(str(file_path))

    async def get_or_create_session(self, session_id=None):
        return session_id or "sess-test"

    async def add_mapping(self, session_id, original_value, entity_type):
        for token, value in self.mappings.items():
            if value == original_value:
                return token
        n = self.counters.get(entity_type, 0) + 1
        self.counters[entity_type] = n
        token = f"[{entity_type}_{n}]"
        self.mappings[token] = original_value
        return token

    async def get_all_mappings(self, session_id):
        return dict(self.mappings)

    async def save_anonymized_text(self, session_id, text, prefix="anonymized_request"):
        return Path(f"data/anonymized_files/{session_id}/{prefix}_fake.md")

    async def log_request(self, **kwargs):
        pass


class FakeOpenRouter:
    """Запоминает аргументы вызова и возвращает заготовленный ответ"""

    def __init__(self, response=None, stream_chunks=None):
        self.response = response or {}
        self.stream_chunks = stream_chunks or []
        self.captured = {}

    async def chat_completion(self, messages, model=None, **kwargs):
        self.captured = {"messages": messages, "model": model, **kwargs}
        return self.response

    async def chat_completion_stream(self, messages, model=None, **kwargs):
        self.captured = {"messages": messages, "model": model, **kwargs}
        for chunk in self.stream_chunks:
            yield chunk


def make_handler(pii, response=None, stream_chunks=None):
    return RequestHandler(
        ner_service=FakeNER(pii),
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(response, stream_chunks),
    )


# ==================== Тесты ====================

def test_schema_preserves_tools():
    """Поля tools/tool_choice/tool_calls/tool_call_id не теряются схемой"""
    body = {
        "model": "test-model",
        "stream": True,
        "tools": [{
            "type": "function",
            "function": {
                "name": "extract_document_text",
                "parameters": {"type": "object"},
            },
        }],
        "tool_choice": "auto",
        "messages": [
            {"role": "user", "content": "Прочитай файл"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "extract_document_text",
                    "arguments": "{\"uri\": \"file:///C:/doc.docx\"}"
                }},
            ]},
            {"role": "tool", "tool_call_id": "call_1",
             "name": "extract_document_text",
             "content": "Содержимое документа"},
        ],
    }
    req = ChatCompletionRequest(**body)
    assert req.tools and req.tools[0]["function"]["name"] == "extract_document_text"
    assert req.tool_choice == "auto"
    assert req.messages[1].content is None
    assert req.messages[1].tool_calls[0]["id"] == "call_1"
    assert req.messages[2].tool_call_id == "call_1"
    print("TEST 1 OK: схема сохраняет tools/tool_choice/tool_calls/tool_call_id")


async def test_anonymize_messages_structure():
    """Анонимизация сохраняет структуру tool_calls и анонимизирует аргументы"""
    handler = make_handler({
        "Ивана Петрова": "PERSON",
        "ООО Ромашка": "ORG",
    })
    request = ChatCompletionRequest(
        model="m",
        mode="full",
        messages=[
            ChatMessage(role="system", content="Ты помощник"),
            ChatMessage(role="user", content="Проанализируй документ от Ивана Петрова"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "call_1", "type": "function", "function": {
                    "name": "extract_document_text",
                    "arguments": json.dumps(
                        {"uri": "file:///c:/doc.docx", "comment": "документ Ивана Петрова"},
                        ensure_ascii=False
                    ),
                }},
            ]),
            ChatMessage(role="tool", tool_call_id="call_1",
                        name="extract_document_text",
                        content="Заявление от Ивана Петрова в ООО Ромашка"),
        ],
    )
    anonymized, entities, _, _ = await handler._anonymize_messages(request, "sess-test")

    # 1) обычный контент анонимизирован
    assert anonymized[1]["content"] == "Проанализируй документ от [PERSON_1]", anonymized[1]["content"]

    # 2) assistant: content=None сохранён, структура tool_calls цела,
    #    аргументы анонимизированы
    a = anonymized[2]
    assert a["content"] is None
    tc = a["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "extract_document_text"
    args = json.loads(tc["function"]["arguments"])
    assert args["uri"] == "file:///c:/doc.docx"
    assert args["comment"] == "документ [PERSON_1]", args

    # 3) tool-сообщение: tool_call_id и name сохранены, контент анонимизирован
    t = anonymized[3]
    assert t["tool_call_id"] == "call_1"
    assert t["name"] == "extract_document_text"
    assert t["content"] == "Заявление от [PERSON_1] в [ORG_1]", t["content"]

    # 4) в анонимизированных данных не осталось исходных PII
    dump = json.dumps(anonymized, ensure_ascii=False)
    assert "Ивана Петрова" not in dump and "ООО Ромашка" not in dump
    print("TEST 2 OK: анонимизация сохраняет tool_calls/tool_call_id и анонимизирует аргументы")


async def test_handle_chat_completion_tools():
    """Не-стриминг: tools уходят в облако, tool_calls ответа де-анонимизируются"""
    response = {
        "id": "r1", "created": 1, "model": "cloud-model", "usage": {},
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_9", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": "{\"text\": \"Данные о [PERSON_1] и [ORG_1]\"}"
                }}
            ]}
        }],
    }
    handler = make_handler(
        {"Ивана Петрова": "PERSON", "ООО Ромашка": "ORG"},
        response=response
    )
    # Маппинг для [ORG_1] уже существовал в сессии ранее
    await handler.store.add_mapping("sess-test", "ООО Ромашка", "ORG")

    tools = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]
    request = ChatCompletionRequest(
        model="m", mode="full", stream=False,
        tools=tools, tool_choice="auto",
        messages=[ChatMessage(role="user", content="Запиши данные про Ивана Петрова")],
    )
    resp, session_id = await handler.handle_chat_completion(request)

    # tools проброшены в облако
    captured = handler.openrouter.captured
    assert captured.get("tools") == tools, f"tools не проброшены: {captured.get('tools')}"
    assert captured.get("tool_choice") == "auto"
    assert "Ивана Петрова" not in json.dumps(captured["messages"], ensure_ascii=False)

    # tool_calls ответа де-анонимизированы
    msg = resp.choices[0].message
    assert msg.content is None
    args = json.loads(msg.tool_calls[0]["function"]["arguments"])
    assert args["text"] == "Данные о Ивана Петрова и ООО Ромашка", args
    print("TEST 3 OK: tools пробрасываются в облако, tool_calls ответа де-анонимизируются")


async def test_stream_tool_calls_buffering():
    """Стриминг: разорванный плейсхолдер в аргументах tool_calls не утекает"""
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "write_file", "arguments": "{\"text\": \"Привет [PER"}}
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "SON_1]\"}"}}
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    handler = make_handler({"Ивана Петрова": "PERSON"}, stream_chunks=chunks)

    request = ChatCompletionRequest(
        model="m", mode="full", stream=True,
        tools=[{"type": "function", "function": {"name": "write_file", "parameters": {}}}],
        messages=[ChatMessage(role="user", content="Напиши приветствие для Ивана Петрова")],
    )
    prepared = await handler.prepare_chat_request(request, session_id="sess-test")
    assert "[PERSON_1]" in prepared.anonymized_messages[0]["content"]

    events = []
    async for ev in handler.stream_from_prepared(request, prepared):
        events.append(ev)

    # tools проброшены в стриминг-вызов
    assert handler.openrouter.captured.get("tools") == request.tools

    # Собираем фрагменты аргументов из SSE-чанков
    full_args = ""
    for ev in events:
        assert ev.startswith("data: ")
        data = ev[len("data: "):].strip()
        if data == "[DONE]":
            continue
        payload = json.loads(data)
        assert "error" not in payload, f"error chunk: {payload}"
        for choice in payload.get("choices", []):
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                frag = (tc.get("function") or {}).get("arguments", "")
                assert "[PERSON_1]" not in frag and "[PER" not in frag, \
                    f"плейсхолдер утёк в стрим: {frag!r}"
                full_args += frag

    assert full_args == "{\"text\": \"Привет Ивана Петрова\"}", full_args
    print("TEST 4 OK: стриминг tool_calls — буферизация разорванного плейсхолдера")


async def main():
    test_schema_preserves_tools()
    await test_anonymize_messages_structure()
    await test_handle_chat_completion_tools()
    await test_stream_tool_calls_buffering()
    print("\nALL TOOL-CALLING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())