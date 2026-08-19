"""
Функциональные тесты автоматической анонимизации приложенных файлов
в passthrough-режиме («Анонимизируй приложенный файл…»).

Проверяют, что в passthrough-режиме запрос с командой анонимизации и блоком
<file_content path="...">:
1. Перехватывается прокси: файл анонимизируется локальной NER-моделью,
   в облако запрос НЕ уходит, создаётся копия <name>.anonymized.<ext>.
2. Ответ содержит session_id, пути к копии/review и маркер
   [anonymizer:done:<путь>].
3. Запрос без команды анонимизации идёт в облако как обычно (passthrough).
4. Запрос с командой, но без файлов идёт в облако (нечего анонимизировать).
5. Повторный запрос в том же диалоге (маркер done в истории) НЕ
   перехватывается — защита от зацикливания агента.
6. Стриминговый вариант: SSE-чанки + [DONE].
7. Явное anonymize=false отключает перехват.

Запуск: python anonymizer_proxy\\tests\\test_files_auto_anonymize.py (из корня проекта)
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_passthrough import (
    FakeNER, FakeStore, FakeOpenRouter,
)

# Перехват активен только в passthrough-режиме — фиксируем его детерминированно,
# независимо от ANONYMIZER_MODE в .env
handlers_module.CURRENT_MODE = "passthrough"

PII = {"Иван Петров": "PERSON", "+7-900-111-22-33": "PHONE"}
FILE_TEXT = "Договор подготовил Иван Петров, телефон +7-900-111-22-33."


def make_handler(cloud_response=None, stream_chunks=None):
    return RequestHandler(
        ner_service=FakeNER(PII),
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(cloud_response, stream_chunks),
    )


def make_txt_file() -> Path:
    tmp = tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(FILE_TEXT)
    tmp.close()
    return Path(tmp.name)


def make_request(path: Path, text: str, anonymize=True) -> ChatCompletionRequest:
    content = (
        f'{text}\n\n'
        f'<file_content path="{path}">\n'
        f'Error fetching content: binary file\n'
        f'</file_content>'
    )
    return ChatCompletionRequest(
        model="m",
        anonymize=anonymize,
        stream=False,
        messages=[ChatMessage(role="user", content=content)],
    )



async def test_intercept_anonymizes_file_no_cloud():
    """Интент + файл → локальная анонимизация, облако НЕ вызывается"""
    path = make_txt_file()
    anon_path = None
    try:
        handler = make_handler()
        resp, sid = await handler.handle_chat_completion(
            make_request(path, "Анонимизируй приложенный файл и составь его резюме")
        )

        # Облако НЕ вызывалось
        assert not handler.openrouter.captured, handler.openrouter.captured
        # Копия создана рядом с оригиналом, PII заменена плейсхолдерами
        anon_path = path.with_name(f"{path.stem}.anonymized{path.suffix}")
        assert anon_path.exists(), "анонимизированная копия не создана"
        anon_text = anon_path.read_text(encoding="utf-8")
        assert "[PERSON_1]" in anon_text and "[PHONE_1]" in anon_text, anon_text
        assert "Иван Петров" not in anon_text, anon_text
        # Оригинал не тронут
        assert "Иван Петров" in path.read_text(encoding="utf-8")
        # Ответ: session_id, пути, маркер done, режим
        answer = resp.choices[0].message.content
        assert sid in answer, answer
        assert str(path) in answer, answer
        assert f"[anonymizer:done:{path}]" in answer, answer
        meta = resp.anonymization_metadata
        assert meta["mode"] == "files_anonymization", meta
        assert meta["entities_found"] == 2, meta
        assert meta["mappings_count"] == 2, meta
        print("TEST 1 OK: интент+файл — локальная анонимизация, облако не вызвано")
    finally:
        path.unlink(missing_ok=True)
        if anon_path:
            anon_path.unlink(missing_ok=True)


async def test_no_intent_goes_to_cloud():
    """Без команды анонимизации запрос с файлом идёт в облако (passthrough)"""
    path = make_txt_file()
    try:
        handler = make_handler(cloud_response={
            "id": "c-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Резюме"},
                "finish_reason": "stop",
            }],
            "usage": {},
        })
        resp, _ = await handler.handle_chat_completion(
            make_request(path, "Составь резюме приложенного файла")
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough"
        assert not path.with_name(f"{path.stem}.anonymized{path.suffix}").exists()
        print("TEST 2 OK: без интента — обычный passthrough в облако")
    finally:
        path.unlink(missing_ok=True)


async def test_intent_without_file_goes_to_cloud():
    """Команда анонимизации без приложенного файла — нечего перехватывать"""
    handler = make_handler(cloud_response={
        "id": "c-1", "created": 1, "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
        "usage": {},
    })
    request = ChatCompletionRequest(
        model="m",
        anonymize=True,
        stream=False,
        messages=[ChatMessage(role="user", content="Анонимизируй этот текст")],
    )
    resp, _ = await handler.handle_chat_completion(request)
    assert handler.openrouter.captured, "облако не вызвано"
    assert resp.anonymization_metadata["mode"] == "passthrough"
    print("TEST 3 OK: интент без файла — passthrough в облако")


async def test_done_marker_prevents_reinterception():
    """Маркер [anonymizer:done:...] в истории отключает повторный перехват"""
    path = make_txt_file()
    try:
        handler = make_handler(cloud_response={
            "id": "c-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Резюме готово"},
                "finish_reason": "stop",
            }],
            "usage": {},
        })
        first = make_request(path, "Анонимизируй приложенный файл")
        resp1, _ = await handler.handle_chat_completion(first)
        assert not handler.openrouter.captured, "первый запрос ушёл в облако"

        # Второй запрос: история содержит исходный блок + наш ответ с маркером
        history = [
            first.messages[0],
            ChatMessage(role="assistant", content=resp1.choices[0].message.content),
            ChatMessage(role="user", content="Теперь составь резюме"),
        ]
        second = ChatCompletionRequest(
            model="m", anonymize=True, stream=False, messages=history,
        )
        resp2, _ = await handler.handle_chat_completion(second)
        assert handler.openrouter.captured, "второй запрос не ушёл в облако"
        # Повторного перехвата нет (облако вызвано), но текст ответа теперь
        # де-анонимизируется выборочно по сессии из истории диалога
        assert resp2.anonymization_metadata["mode"] == "passthrough_deanonymized"
        print("TEST 4 OK: маркер done — повторного перехвата нет (облако вызвано)")
    finally:
        path.unlink(missing_ok=True)
        path.with_name(f"{path.stem}.anonymized{path.suffix}").unlink(missing_ok=True)


async def test_stream_variant():
    """Стриминг: анонимизация файлов отдаётся SSE-чанками, облако не вызывается"""
    path = make_txt_file()
    anon_path = None
    try:
        handler = make_handler()
        request = make_request(path, "Анонимизируй приложенный файл")
        request.stream = True

        file_paths = handler.detect_attached_files_anonymization(request)
        assert file_paths == [str(path)], file_paths

        prepared = await handler.prepare_files_anonymization(request, file_paths)
        out = []
        async for chunk in handler.stream_files_anonymization(request, prepared):
            out.append(chunk)

        assert not handler.openrouter.captured, "облако вызвано при стриме"
        assert any("[DONE]" in c for c in out), out
        # Собираем контент из дельт
        text = ""
        meta = None
        for raw in out:
            if not raw.startswith("data: ") or "[DONE]" in raw:
                continue
            payload = json.loads(raw[len("data: "):])
            delta = payload["choices"][0]["delta"]
            text += delta.get("content", "")
            if "anonymization_metadata" in payload:
                meta = payload["anonymization_metadata"]
        assert prepared.session_id in text, text
        assert f"[anonymizer:done:{path}]" in text, text
        assert meta and meta["mode"] == "files_anonymization", meta
        anon_path = path.with_name(f"{path.stem}.anonymized{path.suffix}")
        assert anon_path.exists()
        print("TEST 5 OK: стрим — SSE-чанки с результатом, облако не вызвано")
    finally:
        path.unlink(missing_ok=True)
        if anon_path:
            anon_path.unlink(missing_ok=True)


async def test_anonymize_false_disables_intercept():
    """Явное anonymize=false отключает и перехват тоже"""
    path = make_txt_file()
    try:
        handler = make_handler(cloud_response={
            "id": "c-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {},
        })
        resp, _ = await handler.handle_chat_completion(
            make_request(path, "Анонимизируй приложенный файл", anonymize=False)
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough"
        assert not path.with_name(f"{path.stem}.anonymized{path.suffix}").exists()
        print("TEST 6 OK: anonymize=false — перехват отключён, облако вызвано")
    finally:
        path.unlink(missing_ok=True)


async def main():
    await test_intercept_anonymizes_file_no_cloud()
    await test_no_intent_goes_to_cloud()
    await test_intent_without_file_goes_to_cloud()
    await test_done_marker_prevents_reinterception()
    await test_stream_variant()
    await test_anonymize_false_disables_intercept()
    print("\nALL FILES AUTO-ANONYMIZE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
