"""
Функциональные тесты авто-де-анонимизации файлов по естественной команде
в passthrough-режиме («деанонимизируй упомянутые файлы»).

Проверяют, что когда в истории диалога есть маркер анонимизации
([anonymizer:result:<путь>] + session_id), а пользователь просит
де-анонимизировать файлы:
1. Де-анонимизируется ФАЙЛ РЕЗУЛЬТАТА (плейсхолдеры → реальные значения),
   облако НЕ вызывается; анонимизированная копия остаётся нетронутой.
2. Без команды де-анонимизации — обычный passthrough.
3. anonymize=false отключает перехват.
4. Если файл результата не создан — информационное сообщение, без падения.

Запуск: python anonymizer_proxy\\tests\\test_deanonymize_intercept.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler, _result_path_for
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


def make_history(copy_path, result_path, command, anonymize=True) -> ChatCompletionRequest:
    messages = [
        ChatMessage(role="user", content="Анонимизируй приложенный файл"),
        ChatMessage(role="assistant", content=(
            "[ANONYMIZER] Приложенные файлы анонимизированы локальной "
            "NER-моделью.\n\n"
            "session_id: sess-test\n\n"
            "Файлы:\n"
            f"- C:/orig.docx → {copy_path} (сущностей: 1)\n"
            "  [anonymizer:done:C:/orig.docx]\n"
            f"  [anonymizer:copy:{copy_path}]\n"
            f"  [anonymizer:result:{result_path}]"
        )),
        ChatMessage(role="user", content=command),
    ]
    return ChatCompletionRequest(
        model="m", anonymize=anonymize, stream=False, messages=messages,
    )


def make_copy_file():
    """Создать анонимизированную копию с плейсхолдером, вернуть путь."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anonymized.txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("Подписант: [PERSON_1]")
    tmp.close()
    return Path(tmp.name)


def make_result_file(copy_path: Path):
    """Создать файл результата с плейсхолдером рядом с копией."""
    result_path = Path(_result_path_for(str(copy_path)))
    result_path.write_text("Подписант: [PERSON_1]", encoding="utf-8")
    return result_path


async def test_deanonymize_result_file():
    """Де-анонимизируется файл результата, копия остаётся нетронутой"""
    copy_path = make_copy_file()
    result_path = make_result_file(copy_path)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(copy_path, result_path, "Деанонимизируй упомянутые файлы")
        )
        assert not handler.openrouter.captured, "облако вызвано"
        assert result_path.read_text(encoding="utf-8") == "Подписант: Иван Петров"
        assert copy_path.read_text(encoding="utf-8") == "Подписант: [PERSON_1]", "копия изменена"
        assert resp.anonymization_metadata["mode"] == "files_deanonymization"
        print("TEST 1 OK: де-анонимизирован файл результата, копия не тронута")
    finally:
        copy_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


async def test_no_command_passthrough():
    """Без команды де-анонимизации — обычный passthrough в облако"""
    copy_path = make_copy_file()
    result_path = make_result_file(copy_path)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(copy_path, result_path, "Составь резюме")
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough_deanonymized"
        assert result_path.read_text(encoding="utf-8") == "Подписант: [PERSON_1]"
        print("TEST 2 OK: без команды — passthrough, файл результата не де-анонимизирован")
    finally:
        copy_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


async def test_anonymize_false_disables():
    """anonymize=false отключает перехват де-анонимизации"""
    copy_path = make_copy_file()
    result_path = make_result_file(copy_path)
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(copy_path, result_path, "Деанонимизируй файлы", anonymize=False)
        )
        assert handler.openrouter.captured, "облако не вызвано"
        assert resp.anonymization_metadata["mode"] == "passthrough"
        assert result_path.read_text(encoding="utf-8") == "Подписант: [PERSON_1]"
        print("TEST 3 OK: anonymize=false — перехват де-анонимизации отключён")
    finally:
        copy_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


async def test_fallback_to_copy():
    """Файл результата не создан, но копия есть → де-анонимизация в отдельный файл результата.

    Копия остаётся нетронутым исходником (инвариант .clinerules), а
    де-анонимизированный текст пишется в <name>.result.<ext>.
    """
    copy_path = make_copy_file()
    result_path = Path(_result_path_for(str(copy_path)))
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(copy_path, result_path, "Деанонимизируй упомянутые файлы")
        )
        assert not handler.openrouter.captured, "облако вызвано"
        # fallback: копия НЕ тронута, результат — в отдельном файле
        assert copy_path.read_text(encoding="utf-8") == "Подписант: [PERSON_1]", "копия изменена"
        assert result_path.read_text(encoding="utf-8") == "Подписант: Иван Петров", "результат не де-анонимизирован"
        assert "де-анонимизирована анонимизированная копия" in resp.choices[0].message.content
        print("TEST 4 OK: файл результата отсутствует — де-анонимизация в отдельный файл (fallback)")
    finally:
        copy_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


async def test_both_missing_note():
    """Нет ни файла результата, ни копии — информационное сообщение"""
    copy_path = make_copy_file()
    result_path = Path(_result_path_for(str(copy_path)))
    copy_path.unlink()  # удаляем копию тоже
    try:
        handler = make_handler()
        resp, _ = await handler.handle_chat_completion(
            make_history(copy_path, result_path, "Деанонимизируй упомянутые файлы")
        )
        assert not handler.openrouter.captured, "облако вызвано"
        assert "файл результата не найден" in resp.choices[0].message.content
        print("TEST 5 OK: нет ни результата, ни копии — корректное сообщение")
    finally:
        copy_path.unlink(missing_ok=True)


async def main():
    await test_deanonymize_result_file()
    await test_no_command_passthrough()
    await test_anonymize_false_disables()
    await test_fallback_to_copy()
    await test_both_missing_note()
    print("\nALL DEANONYMIZE INTERCEPT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
