"""
Функциональные тесты режима anonymize_only для chat completions.

Проверяют ожидаемое поведение после рефакторинга канонического результата:
1. Запрос НЕ отправляется в облако (OpenRouter не вызывается).
2. Канонический анонимизированный результат сохраняется в файл.
3. Ответ содержит служебную строку техсведений и канонический результат
   между маркерами RESULT_BEGIN/RESULT_END (без JSON-дампа сообщений).
4. Содержимое файла идентично результату между маркерами.
5. Техинформация дублируется в anonymization_metadata.

Запуск: python anonymizer_proxy\\tests\\test_anonymize_only.py (из корня проекта)
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.config import RESULT_BEGIN, RESULT_END
from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import (
    FakeNER, FakeStore, FakeOpenRouter,
)


class FileStore(FakeStore):
    """FakeStore + фиксация вызовов save_anonymized_text"""

    def __init__(self):
        super().__init__()
        self.saved = []

    async def save_anonymized_text(self, session_id, text, prefix="anonymized_request"):
        self.saved.append((session_id, prefix, text))
        return Path(f"data/anonymized_files/{session_id}/{prefix}_fake.md")


def extract_result(content: str) -> str:
    """Извлечь канонический результат из ответа по маркерам"""
    m = re.search(
        re.escape(RESULT_BEGIN) + r"\n(.*?)\n" + re.escape(RESULT_END),
        content,
        re.DOTALL,
    )
    assert m, f"маркеры результата не найдены в ответе: {content!r}"
    return m.group(1)


def make_anon_handler():
    store = FileStore()
    handler = RequestHandler(
        ner_service=FakeNER({"Ивана Петрова": "PERSON"}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(),
    )
    return handler, store


def make_request(stream: bool):
    return ChatCompletionRequest(
        model="m",
        mode="anonymize_only",
        stream=stream,
        messages=[
            ChatMessage(role="system", content="СЕКРЕТНЫЙ СИСТЕМНЫЙ ПРОМПТ"),
            ChatMessage(role="user", content="Проверь документ от Ивана Петрова"),
        ],
    )


async def test_anonymize_only_no_cloud():
    """Не-стриминг: облако не вызывается, файл сохраняется, канонический
    результат между маркерами, техинформация в anonymization_metadata"""
    handler, store = make_anon_handler()
    resp, session_id = await handler.handle_chat_completion(make_request(stream=False))

    assert handler.openrouter.captured == {}, "запрос был отправлен в облако!"
    assert len(store.saved) == 1, "анонимизированный файл не сохранён"
    sid, prefix, text = store.saved[0]
    assert "[PERSON_1]" in text and "Ивана Петрова" not in text, text
    assert "СЕКРЕТНЫЙ СИСТЕМНЫЙ ПРОМПТ" not in text, "системная часть попала в файл"
    assert "## system" not in text, "системные сообщения не должны сохраняться"

    content = resp.choices[0].message.content

    # Маркеры присутствуют, внутри — канонический markdown (не JSON-дамп)
    assert RESULT_BEGIN in content and RESULT_END in content
    result = extract_result(content)
    assert not result.lstrip().startswith("["), \
        f"вместо markdown — JSON-дамп: {result[:120]!r}"
    assert result == text, "результат между маркерами != содержимому файла"

    # Служебная строка техсведений — до маркеров
    summary = content.split(RESULT_BEGIN)[0]
    assert session_id in summary
    assert "anonymize_only" in summary
    assert "fake.md" in summary, "путь к файлу отсутствует в техсводке"

    # Техинформация дублируется в anonymization_metadata
    meta = resp.anonymization_metadata
    assert meta["mode"] == "anonymize_only"
    assert meta["session_id"] == session_id
    assert meta["anonymized_request_file"].endswith("fake.md")
    assert meta["entities_found"] == 1
    assert meta["mappings_count"] == 1

    assert "[PERSON_1]" in content and "Ивана Петрова" not in content
    assert "СЕКРЕТНЫЙ СИСТЕМНЫЙ ПРОМПТ" not in content, "системная часть попала в ответ"
    print("TEST 1 OK: anonymize_only (не-стрим) — маркеры, файл, metadata")


async def test_anonymize_only_stream():
    """Стриминг: облако не вызывается, файл сохраняется, SSE содержит
    маркеры и канонический markdown; metadata в финальном чанке"""
    handler, store = make_anon_handler()
    request = make_request(stream=True)
    prepared = await handler.prepare_chat_request(request, session_id=None)

    events = []
    async for ev in handler.stream_from_prepared(request, prepared):
        events.append(ev)

    assert handler.openrouter.captured == {}, "запрос был отправлен в облако!"
    assert len(store.saved) == 1, "анонимизированный файл не сохранён"

    full = ""
    last_metadata = None
    for ev in events:
        data = ev[len("data: "):].strip()
        if data == "[DONE]":
            continue
        payload = json.loads(data)
        if payload.get("anonymization_metadata"):
            last_metadata = payload["anonymization_metadata"]
        for choice in payload.get("choices", []):
            frag = (choice.get("delta") or {}).get("content")
            if frag:
                full += frag

    # Маркеры в собранном стриме, внутри — канонический markdown
    assert RESULT_BEGIN in full and RESULT_END in full
    result = extract_result(full)
    sid, prefix, text = store.saved[0]
    assert result == text, "результат в стриме != содержимому файла"
    assert not result.lstrip().startswith("["), \
        f"вместо markdown — JSON-дамп: {result[:120]!r}"
    assert "[PERSON_1]" in full and "Ивана Петрова" not in full
    assert "СЕКРЕТНЫЙ СИСТЕМНЫЙ ПРОМПТ" not in full, "системная часть попала в стрим"

    # Metadata в финальном чанке
    assert last_metadata is not None, "метаданные не найдены в SSE"
    assert last_metadata["mode"] == "anonymize_only"
    assert last_metadata["anonymized_request_file"].endswith("fake.md")
    assert last_metadata["entities_found"] == 1
    print("TEST 2 OK: anonymize_only (стрим) — маркеры, файл, metadata")


def make_full_handler(response=None, stream_chunks=None):
    store = FileStore()
    handler = RequestHandler(
        ner_service=FakeNER({"Ивана Петрова": "PERSON"}),
        mapping_store=store,
        openrouter_client=FakeOpenRouter(response, stream_chunks),
    )
    return handler, store


FULL_RESPONSE = {
    "id": "r1", "created": 1, "model": "cloud-model", "usage": {},
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "Ответ про [PERSON_1]"},
    }],
}


async def test_full_mode_saves_file_no_stream():
    """Full-режим (не-стрим): канонический результат сохраняется в файл,
    путь в anonymization_metadata, содержимое файла — проекция контекста,
    реально ушедшего в облако"""
    handler, store = make_full_handler(response=FULL_RESPONSE)
    request = ChatCompletionRequest(
        model="m", mode="full", stream=False,
        messages=[
            ChatMessage(role="system", content="Ты помощник"),
            ChatMessage(role="user", content="Проверь документ от Ивана Петрова"),
        ],
    )
    resp, session_id = await handler.handle_chat_completion(request)

    # Облако вызвано с анонимизированным контекстом
    captured = handler.openrouter.captured
    assert captured.get("messages"), "облако не вызвано"
    assert "[PERSON_1]" in json.dumps(captured["messages"], ensure_ascii=False)

    # Файл сохранён и в full-режиме
    assert len(store.saved) == 1, "в full-режиме файл не сохранён"
    sid, prefix, text = store.saved[0]

    # Содержимое файла — markdown-проекция именно того контекста,
    # который ушёл в облако (включая системные сообщения)
    from anonymizer_proxy.proxy.handlers import _render_messages_markdown
    assert text == _render_messages_markdown(captured["messages"]), \
        "файл не совпадает с проекцией отправленного в облако контекста"
    assert "## system" in text, "в full-режиме системные сообщения входят в файл"
    assert "Ивана Петрова" not in text

    # Путь к файлу в метаданных ответа
    meta = resp.anonymization_metadata
    assert meta["mode"] == "full"
    assert meta["anonymized_request_file"].endswith("fake.md")
    assert meta["session_id"] == session_id
    print("TEST 3 OK: full (не-стрим) — файл сохранён, проекция = контексту облака")


async def test_full_mode_saves_file_stream():
    """Full-режим (стрим): файл сохраняется, metadata-чанк с путём
    приходит ДО [DONE]"""
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "Привет, [PERSON_1]!"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    handler, store = make_full_handler(stream_chunks=chunks)
    request = ChatCompletionRequest(
        model="m", mode="full", stream=True,
        messages=[ChatMessage(role="user", content="Напиши привет для Ивана Петрова")],
    )
    prepared = await handler.prepare_chat_request(request, session_id=None)

    events = []
    async for ev in handler.stream_from_prepared(request, prepared):
        events.append(ev)

    assert len(store.saved) == 1, "в full-стриме файл не сохранён"
    sid, prefix, text = store.saved[0]

    from anonymizer_proxy.proxy.handlers import _render_messages_markdown
    captured = handler.openrouter.captured
    assert text == _render_messages_markdown(captured["messages"]), \
        "файл не совпадает с проекцией отправленного в облако контекста"

    # Metadata-чанк присутствует и идёт ДО [DONE]
    done_index = next(i for i, ev in enumerate(events) if "[DONE]" in ev)
    metadata_index = None
    last_metadata = None
    for i, ev in enumerate(events):
        data = ev[len("data: "):].strip()
        if data == "[DONE]":
            continue
        payload = json.loads(data)
        if payload.get("anonymization_metadata"):
            metadata_index = i
            last_metadata = payload["anonymization_metadata"]
    assert metadata_index is not None, "metadata-чанк не найден в full-стриме"
    assert metadata_index < done_index, "metadata-чанк пришёл после [DONE]"
    assert last_metadata["mode"] == "full"
    assert last_metadata["anonymized_request_file"].endswith("fake.md")

    # Ответ де-анонимизирован (реальный текст, а не плейсхолдер)
    content = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for ev in events[:done_index]
        for choice in json.loads(ev[len("data: "):].strip()).get("choices", [])
    )
    assert "Ивана Петрова" in content and "[PERSON_1]" not in content
    print("TEST 4 OK: full (стрим) — файл сохранён, metadata до [DONE]")


async def test_file_content_error_block_extracted():
    """
    Блок <file_content>, который клиент не смог прочитать
    ("Error fetching content: Cannot read binary file into context."),
    подменяется текстом, извлечённым прокси из локального DOCX-файла,
    и этот текст проходит NER/анонимизацию.
    """
    import tempfile
    from docx import Document

    # Создаём реальный DOCX с PII
    doc = Document()
    doc.add_paragraph(
        "Генеральному директору ООО «Завод» от бухгалтера Ивана Петрова"
    )
    with tempfile.NamedTemporaryFile(
        suffix=".docx", delete=False
    ) as tmp:
        doc.save(tmp)
        docx_path = Path(tmp.name)

    try:
        handler, store = make_anon_handler()
        path_str = str(docx_path).replace("\\", "/")
        request = ChatCompletionRequest(
            model="m", mode="anonymize_only", stream=False,
            messages=[
                ChatMessage(
                    role="user",
                    content=(
                        "от кого и кому адресован приложенный документ?\n\n"
                        f'<file_content path="{path_str}">\n'
                        "Error fetching content: Cannot read binary file "
                        "into context.\n"
                        "</file_content>"
                    ),
                ),
            ],
        )
        resp, session_id = await handler.handle_chat_completion(request)

        content = resp.choices[0].message.content
        result = extract_result(content)

        # Заглушка-ошибка заменена извлечённым текстом документа
        assert "Error fetching content" not in result, result
        # Текст документа извлечён и анонимизирован
        assert "ЗАЯВЛЕНИЕ" not in result  # в документе этого слова нет
        assert "Генеральному директору" in result, result
        assert "[PERSON_1]" in result, result
        assert "Ивана Петрова" not in result, \
            "PII из документа не анонимизирована"
        # Файл с каноническим результатом содержит то же самое
        assert store.saved and store.saved[0][2] == result
        print("TEST 5 OK: <file_content> с ошибкой клиента — "
              "содержимое DOCX извлечено и анонимизировано")
    finally:
        docx_path.unlink(missing_ok=True)


async def test_file_content_error_block_missing_file_kept():
    """Если локальный файл не найден, исходный блок с ошибкой клиента
    остаётся без изменений (запрос не падает)"""
    handler, store = make_anon_handler()
    missing = "c:/definitely/not/existing/path_12345.docx"
    request = ChatCompletionRequest(
        model="m", mode="anonymize_only", stream=False,
        messages=[
            ChatMessage(
                role="user",
                content=(
                    "Что в файле?\n\n"
                    f'<file_content path="{missing}">\n'
                    "Error fetching content: Cannot read binary file "
                    "into context.\n"
                    "</file_content>"
                ),
            ),
        ],
    )
    resp, _ = await handler.handle_chat_completion(request)
    result = extract_result(resp.choices[0].message.content)
    assert "Error fetching content" in result, result
    assert missing in result, result
    print("TEST 6 OK: несуществующий файл — блок оставлен без изменений")


async def main():
    await test_anonymize_only_no_cloud()
    await test_anonymize_only_stream()
    await test_full_mode_saves_file_no_stream()
    await test_full_mode_saves_file_stream()
    await test_file_content_error_block_extracted()
    await test_file_content_error_block_missing_file_kept()
    print("\nALL ANONYMIZE_ONLY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
