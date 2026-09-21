"""
Тесты авто-продолжения «пустого» ответа локальной thinking-модели.

РЕГРЕССИЯ (наблюдение из Cline Desktop): qwen3.5-9b иногда завершает
генерацию внутри блока размышлений — весь ответ (включая задуманный
tool-call или готовый черновик) остаётся в reasoning_content/thinking-тегах,
content пуст и tool_calls нет. Клиент-агент (Cline) получает ход без текста
и без tool-call и молча ждёт ввода: интерфейс «зависает», GPU простаивает,
результат появляется только после нового сообщения пользователя.

Проверяют:
1. Детекция «пустого» ответа: reasoning_content, thinking-теги, нет tool_calls.
2. Non-stream: thinking-only ответ -> ровно один дозапрос с подсказкой,
   клиенту возвращается продолжение; обычный ответ/tool_calls не дозапрашиваются.
3. Stream: только размышления -> дозапрос в том же SSE-потоке.
4. Облачный бэкенд не дозапрашивается (проблема локальных thinking-моделей).

Запуск: python anonymizer_proxy\\tests\\test_empty_response_retry.py (из корня проекта)

Теги размышлений собираются из ASCII-кусков: литералы в исходнике легко
повреждаются копированием/кодировками, а такой способ устойчив.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.config import RUNTIME
from anonymizer_proxy.proxy.llm_router import (
    LLMRouter, _StreamScan, _empty_local_response, _strip_think,
)

T_OPEN = "<" + "think" + ">"
T_CLOSE = "<" + "/" + "think" + ">"


class FakeLocal:
    """Локальный «клиент»: отдаёт заготовленные ответы по очереди."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_completion(self, messages, model=None, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)

    async def chat_completion_stream(self, messages, model=None, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        for chunk in self.responses.pop(0):
            yield chunk


# «Пустой» ответ: модель ушла в размышления и завершила генерацию
# (как в логах Cline: в thinking остались даже обрывки tool-call)
THINKING_ONLY = {
    "id": "r1", "created": 1, "model": "local", "usage": {},
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": None,
                    "reasoning_content": "думаю… <" + "/parameter></function>"},
        "finish_reason": "stop",
    }],
}

# «Пустой» ответ в другом виде: теги размышлений пришли в content
THINK_TAGS_ONLY = {
    "id": "r2", "created": 2, "model": "local", "usage": {},
    "choices": [{
        "index": 0,
        "message": {"role": "assistant",
                    "content": T_OPEN + "думаю… " + T_CLOSE},
        "finish_reason": "stop",
    }],
}

REAL_ANSWER = {
    "id": "r3", "created": 3, "model": "local", "usage": {},
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Вот результат"},
        "finish_reason": "stop",
    }],
}

TOOL_CALL = {
    "id": "r4", "created": 4, "model": "local", "usage": {},
    "choices": [{
        "index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "search_local_files",
                          "arguments": '{"query": "тест"}'}}]},
    }],
}


def make_router(local):
    router = LLMRouter()
    router._local = local
    return router


def test_strip_think_and_empty_detection():
    assert _strip_think(T_OPEN + "думаю" + T_CLOSE + "Ответ") == "Ответ"
    assert _strip_think(T_OPEN + "думаю…") == ""
    assert _strip_think("  ") == ""
    assert _strip_think("Ответ") == "Ответ"
    assert _empty_local_response(THINKING_ONLY)
    assert _empty_local_response(THINK_TAGS_ONLY)
    assert not _empty_local_response(REAL_ANSWER)
    assert not _empty_local_response(TOOL_CALL)
    rc_only = {"choices": [{"message": {"role": "assistant",
                                        "content": None,
                                        "reasoning_content": "думаю…"}}]}
    assert _empty_local_response(rc_only)
    print("TEST 1 OK: детекция пустого ответа (reasoning/thinking-теги/tool_calls)")


def test_nonstream_retry():
    local = FakeLocal([THINKING_ONLY, REAL_ANSWER])
    router = make_router(local)
    data = asyncio.run(router.chat_completion(
        messages=[{"role": "user", "content": "вопрос"}],
        model="local/x", backend="local"))
    assert len(local.calls) == 2, "должен быть ровно один дозапрос"
    assert data["choices"][0]["message"]["content"] == "Вот результат"
    nudged = local.calls[1]["messages"][-1]
    assert nudged["role"] == "user" and "Продолжи" in nudged["content"], \
        "в дозапросе должна быть подсказка-продолжение"
    assert local.calls[1]["messages"][:-1] == local.calls[0]["messages"], \
        "исходные сообщения должны сохраниться"
    print("TEST 2 OK: non-stream — thinking-only дозапрашивается с подсказкой")


def test_nonstream_no_retry():
    local = FakeLocal([REAL_ANSWER])
    router = make_router(local)
    asyncio.run(router.chat_completion(
        messages=[{"role": "user", "content": "вопрос"}],
        model="local/x", backend="local"))
    assert len(local.calls) == 1, "нормальный ответ не дозапрашивается"

    local = FakeLocal([TOOL_CALL])
    router = make_router(local)
    asyncio.run(router.chat_completion(
        messages=[{"role": "user", "content": "вопрос"}],
        model="local/x", backend="local"))
    assert len(local.calls) == 1, "tool_calls — не пустой ответ"
    print("TEST 2b OK: контент и tool_calls не дозапрашиваются")


def test_stream_retry():
    think_chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "думаю…"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    answer_chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Ответ"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    local = FakeLocal([think_chunks, answer_chunks])
    router = make_router(local)

    async def run():
        return [c async for c in router.chat_completion_stream(
            messages=[{"role": "user", "content": "вопрос"}],
            model="local/x", backend="local")]

    chunks = asyncio.run(run())
    assert len(local.calls) == 2, "стрим thinking-only должен дозапрашиваться"
    contents = "".join(
        (c["choices"][0].get("delta") or {}).get("content", "")
        for c in chunks if c.get("choices"))
    assert "Ответ" in contents, contents
    assert "Продолжи" in local.calls[1]["messages"][-1]["content"]
    print("TEST 3 OK: stream — thinking-only дозапрашивается в том же SSE")


def test_stream_think_tags_only_retry():
    """Размышления пришли в content (теги) — тоже «пустой» ответ."""
    chunks = [{"choices": [{"index": 0,
                            "delta": {"content": T_OPEN + "думаю…"},
                            "finish_reason": None}]}]
    local = FakeLocal([chunks, chunks])
    router = make_router(local)

    async def run():
        return [c async for c in router.chat_completion_stream(
            messages=[{"role": "user", "content": "вопрос"}],
            model="local/x", backend="local")]

    asyncio.run(run())
    assert len(local.calls) == 2, "content только с тегами — пустой ответ"
    print("TEST 3b OK: stream — контент только из размышлений = дозапрос")


def test_stream_scan():
    scan = _StreamScan()
    scan.feed({"choices": [{"delta": {"reasoning_content": "думаю"}}]})
    assert scan.empty, "только reasoning — пусто"
    scan.feed({"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]})
    assert not scan.empty, "tool_calls видны"

    scan2 = _StreamScan()
    scan2.feed({"choices": [{"delta": {"content": T_OPEN + "x" + T_CLOSE}}]})
    assert scan2.empty, "закрытый блок размышлений — пусто"
    scan2.feed({"choices": [{"delta": {"content": "Текст"}}]})
    assert not scan2.empty, "обычный текст виден"
    print("TEST 4 OK: _StreamScan — reasoning/теги пусты, текст и tool_calls нет")


def test_cloud_not_retried():
    """Облачный бэкенд не дозапрашивается: проблема локальных thinking-моделей."""
    from anonymizer_proxy.tests.test_tools_manual import FakeOpenRouter

    class OneShot(FakeOpenRouter):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def chat_completion(self, messages, model=None, **kwargs):
            self.n += 1
            return dict(THINKING_ONLY)

    cloud = OneShot()
    router = LLMRouter()
    router._openrouter = cloud
    old_backend, old_cloud = RUNTIME["backend"], RUNTIME.get("cloud")
    try:
        RUNTIME["backend"] = "openrouter"
        RUNTIME["cloud"] = "openrouter"
        asyncio.run(router.chat_completion(
            messages=[{"role": "user", "content": "вопрос"}],
            model="m", backend="openrouter"))
        assert cloud.n == 1, "облачный бэкенд не должен дозапрашиваться"
        print("TEST 5 OK: облако — без авто-продолжения")
    finally:
        RUNTIME["backend"] = old_backend
        RUNTIME["cloud"] = old_cloud


def main():
    test_strip_think_and_empty_detection()
    test_nonstream_retry()
    test_nonstream_no_retry()
    test_stream_retry()
    test_stream_think_tags_only_retry()
    test_stream_scan()
    test_cloud_not_retried()
    print("\nALL EMPTY-RESPONSE-RETRY TESTS PASSED")


if __name__ == "__main__":
    main()