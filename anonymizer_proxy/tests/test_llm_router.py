"""
Тесты LLMRouter (OpenRouter / локальная LM Studio) и чат-команд управления.

Проверяют:
1. Выбор бэкенда: префиксы local//cloud/, точное имя локальной модели,
   рантайм-дефолт, явный backend-аргумент.
2. set_backend: переключение без перезапуска + персист в runtime_state.json.
3. Thinking локальной модели НЕ вырезается: reasoning_content и
   <think>…</think> доходят до клиента как есть (non-stream и stream).
4. max_tokens клиента не пересылается в LM Studio — у локальной модели
   нет бюджета выходных токенов.
5. Чат-команды «перезапусти прокси» / «работай через локальную модель» /
   «работай через облако» распознаются только в текущем сообщении.
6. На локальном бэкенде авто-анонимизация приложенных файлов отключена —
   запрос уходит в локальную модель (заглушку) без перехвата.

Запуск: python anonymizer_proxy\\tests\\test_llm_router.py (из корня проекта)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.config import LOCAL_LLM, RUNTIME, RUNTIME_STATE_PATH
from anonymizer_proxy.proxy.llm_router import LLMRouter, LocalLMClient
from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy import handlers as handlers_module
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.tests.test_tools_manual import (
    FakeNER, FakeStore, FakeOpenRouter,
)

handlers_module.CURRENT_MODE = "manual"

CLOUD_RESPONSE = {
    "id": "c-1", "created": 1, "model": "m",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {},
}


def make_router() -> LLMRouter:
    router = LLMRouter()
    router._openrouter = FakeOpenRouter(CLOUD_RESPONSE)
    router._local = FakeOpenRouter(CLOUD_RESPONSE)
    return router


def test_backend_resolution():
    """local//cloud-префиксы, имя локальной модели, рантайм-дефолт"""
    router = make_router()
    old_backend = RUNTIME["backend"]
    old_cloud = RUNTIME.get("cloud")
    try:
        RUNTIME["backend"] = "openrouter"
        RUNTIME["cloud"] = "openrouter"
        assert router._resolve(None) == "openrouter"
        assert router._resolve("qwen/qwen-3.7-max") == "openrouter"
        assert router._resolve("local/qwen3.5-9b") == "local"
        assert router._resolve("cloud/qwen") == "openrouter"
        RUNTIME["backend"] = "local"
        assert router._resolve("qwen/qwen-3.7-max") == "local", \
            "дефолт-бэкенд действует, если модель не локальная"
        assert router._resolve("anything", backend="openrouter") == "openrouter"
        print("TEST 1 OK: разрешение бэкенда (local//cloud/, дефолт, явный)")
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud


def test_set_backend_persists():
    """set_backend переключает бэкенд на лету и сохраняет выбор на диск"""
    router = make_router()
    old = RUNTIME["backend"]
    try:
        router.set_backend("local")
        assert RUNTIME["backend"] == "local"
        assert RUNTIME_STATE_PATH.is_file()
        state = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
        assert state["backend"] == "local", state
        router.set_backend("openrouter")
        assert RUNTIME["backend"] == "openrouter"
        try:
            router.set_backend("wrong")
            assert False, "должен быть ValueError"
        except ValueError:
            pass
        print("TEST 2 OK: set_backend — переключение и персист")
    finally:
        RUNTIME["backend"] = old


def test_local_thinking_passthrough():
    """Thinking не вырезается: reasoning_content и <think> доходят до клиента"""
    import httpx

    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "<think>думаю</think>Ответ",
                "reasoning_content": "шаги размышления",
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=resp)

    local = LocalLMClient({"base_url": "http://x/v1", "model": "test-model"})
    local._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    data = asyncio.run(local.chat_completion(
        messages=[{"role": "user", "content": "тест"}]))
    msg = data["choices"][0]["message"]
    assert msg["content"] == "<think>думаю</think>Ответ", msg
    assert msg["reasoning_content"] == "шаги размышления", msg
    print("TEST 3 OK: thinking локальной модели проходит без вырезания")


def test_local_stream_thinking_passthrough():
    """В стриме reasoning_content и <think> не фильтруются"""
    import httpx

    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"думаю"}}]}',
        'data: {"choices":[{"delta":{"content":"<think>x</think>От"}}]}',
        'data: {"choices":[{"delta":{"content":"вет"},'
        '"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    body = "\n\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"})

    local = LocalLMClient({"base_url": "http://x/v1", "model": "test-model"})
    local._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        out = []
        async for chunk in local.chat_completion_stream(
                messages=[{"role": "user", "content": "тест"}]):
            out.append(chunk)
        return out

    chunks = asyncio.run(run())
    deltas = [c["choices"][0].get("delta", {})
              for c in chunks if c.get("choices")]
    assert any("reasoning_content" in d for d in deltas), deltas
    content = "".join(d.get("content", "") for d in deltas)
    assert content == "<think>x</think>Ответ", content
    print("TEST 3b OK: стрим — thinking проходит без фильтра")


def test_local_no_max_tokens():
    """max_tokens клиента не пересылается: у локальной модели нет бюджета"""
    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ок"},
                         "finish_reason": "stop"}],
            "usage": {},
        })

    local = LocalLMClient({"base_url": "http://x/v1", "model": "test-model"})
    local._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    asyncio.run(local.chat_completion(
        messages=[{"role": "user", "content": "тест"}], max_tokens=512))
    assert "max_tokens" not in captured["json"], captured["json"]
    print("TEST 6 OK: max_tokens не пересылается в LM Studio")


def make_request(text: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[ChatMessage(role="user", content=text)],
    )


def test_command_detection():
    """Команды управления распознаются только в текущем сообщении"""
    from anonymizer_proxy.config import RUNTIME
    handler = RequestHandler(
        ner_service=FakeNER({}),
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(CLOUD_RESPONSE),
    )
    assert handler.detect_restart_request(make_request("перезапусти прокси"))
    assert not handler.detect_restart_request(make_request("составь отчёт"))
    assert handler.detect_backend_switch(
        make_request("работай через локальную модель")) == "local"
    # «облако» -> ДЕЙСТВУЮЩИЙ облачный провайдер (для детерминизма теста
    # фиксируем память о последнем выбранном)
    old_cloud = RUNTIME.get("cloud")
    RUNTIME["cloud"] = "openrouter"
    try:
        assert handler.detect_backend_switch(
            make_request("переключись на облако")) == "openrouter"
    finally:
        RUNTIME["cloud"] = old_cloud
    assert handler.detect_backend_switch(
        make_request("перезапусти прокси")) is None

    # Устаревшая команда в истории не срабатывает
    history = ChatCompletionRequest(
        model="m", anonymize=True, stream=False,
        messages=[
            ChatMessage(role="user", content="перезапусти прокси"),
            ChatMessage(role="assistant", content="ок"),
            ChatMessage(role="user", content="составь отчёт"),
        ],
    )
    assert not handler.detect_restart_request(history)
    print("TEST 4 OK: чат-команды — только текущее сообщение пользователя")


def test_local_backend_skips_file_anon():
    """На локальном бэкенде авто-анонимизация файлов отключена:
    запрос с файлом и командой «скрой все данные» уходит в локальную модель"""
    import tempfile

    def make_txt_file() -> Path:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="w", encoding="utf-8")
        tmp.write("Договор подготовил Иван Петров, телефон +7-900-111-22-33.")
        tmp.close()
        return Path(tmp.name)

    def make_request(path: Path) -> ChatCompletionRequest:
        content = (
            "Скрой все данные\n\n"
            f'<file_content path="{path}">\n'
            "Error fetching content: binary file\n"
            "</file_content>"
        )
        return ChatCompletionRequest(
            model="m", anonymize=True, stream=False,
            messages=[ChatMessage(role="user", content=content)],
        )

    path = make_txt_file()
    old = RUNTIME["backend"]
    try:
        handler = RequestHandler(
            ner_service=FakeNER({}),
            mapping_store=FakeStore(),
            openrouter_client=make_router(),  # _openrouter/_local — фейки
        )
        RUNTIME["backend"] = "local"
        resp, _ = asyncio.run(handler.handle_chat_completion(
            make_request(path)))
        # Перехвата не было: запрос ушёл в «локальную модель» (заглушку)
        assert handler.openrouter._local.captured, "запрос не дошёл до локальной модели"
        assert not handler.openrouter._openrouter.captured
        assert not path.with_name(
            f"{path.stem}.anonymized{path.suffix}").exists(), \
            "на локальном бэкенде создавать анонимизированную копию нельзя"

        RUNTIME["backend"] = "openrouter"
        handler2 = RequestHandler(
            ner_service=FakeNER({}),
            mapping_store=FakeStore(),
            openrouter_client=make_router(),
        )
        resp2, _ = asyncio.run(handler2.handle_chat_completion(
            make_request(path)))
        assert not handler2.openrouter._openrouter.captured, \
            "на облачном бэкенде перехват должен сработать"
        assert path.with_name(
            f"{path.stem}.anonymized{path.suffix}").exists()
        print("TEST 5 OK: local — без авто-анонимизации; openrouter — с ней")
    finally:
        RUNTIME["backend"] = old
        path.unlink(missing_ok=True)
        path.with_name(f"{path.stem}.anonymized{path.suffix}").unlink(
            missing_ok=True)


def main():
    test_backend_resolution()
    test_set_backend_persists()
    test_local_thinking_passthrough()
    test_local_stream_thinking_passthrough()
    test_local_no_max_tokens()
    test_command_detection()
    test_local_backend_skips_file_anon()
    print("\nALL LLM ROUTER TESTS PASSED")


if __name__ == "__main__":
    main()
