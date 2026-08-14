"""
Функциональные тесты режима ручного ревью (mode="review").

Проверяют:
1. ReviewQueue: add_pending/get_pending_list/approve/reject/wait_for_decision.
2. Не-стриминг: запрос приостанавливается, появляется в очереди; после approve
   без правок в облако уходят анонимизированные сообщения.
3. Не-стриминг: approve с отредактированным контентом отправляет его как единое
   user-сообщение.
4. Не-стриминг: reject поднимает ReviewRejectedError и не вызывает облако.
5. Стриминг: approve разблокирует поток, сообщения уходят в облако.

Запуск: python anonymizer_proxy\\tests\\test_manual_review.py (из корня проекта)
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import ChatCompletionRequest, ChatMessage
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.proxy.review_queue import ReviewQueue, ReviewRejectedError
from anonymizer_proxy.tests.test_tools_passthrough import (
    FakeNER, FakeStore, FakeOpenRouter,
)


def make_review_handler(review_queue=None, response=None, stream_chunks=None):
    """Создать RequestHandler с ReviewQueue и фейковыми зависимостями"""
    handler = RequestHandler(
        ner_service=FakeNER({"Ивана Петрова": "PERSON"}),
        mapping_store=FakeStore(),
        openrouter_client=FakeOpenRouter(response, stream_chunks),
        review_queue=review_queue or ReviewQueue(),
    )
    return handler, handler.review_queue, handler.openrouter


def make_request(stream=False):
    return ChatCompletionRequest(
        model="m",
        mode="review",
        stream=stream,
        messages=[
            ChatMessage(role="user", content="Проверь документ от Ивана Петрова"),
        ],
    )


async def wait_for_pending(rq, expected=1, timeout=3.0):
    """Опрашивать очередь, пока не появится ожидаемое число записей"""
    end = time.time() + timeout
    while time.time() < end:
        pending = await rq.get_pending_list()
        if len(pending) >= expected:
            return pending
        await asyncio.sleep(0.01)
    raise AssertionError("запрос не появился в очереди ревью за отведённое время")


# ==================== Тесты ReviewQueue ====================

async def test_review_queue_lifecycle():
    rq = ReviewQueue()
    p = await rq.add_pending("s1", "r1", Path("x.md"))
    assert p.approved is False

    pending = await rq.get_pending_list()
    assert len(pending) == 1 and pending[0]["request_id"] == "r1"

    # Одобрение разблокирует ожидание
    waiter = asyncio.create_task(rq.wait_for_decision("r1"))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    ok = await rq.approve("r1", edited_content="отредактировано")
    assert ok
    decision = await waiter
    assert decision.approved and decision.edited_content == "отредактировано"

    # Обработанный запрос не виден в pending и повторный approve невозможен
    assert await rq.get_pending_list() == []
    assert await rq.approve("r1") is False
    print("TEST 1 OK: ReviewQueue — add/pending/approve/wait")


async def test_review_queue_reject():
    rq = ReviewQueue()
    await rq.add_pending("s1", "r1", Path("x.md"))
    waiter = asyncio.create_task(rq.wait_for_decision("r1"))
    await asyncio.sleep(0.01)
    ok = await rq.reject("r1", "не нужно")
    assert ok
    decision = await waiter
    assert not decision.approved and decision.rejection_reason == "не нужно"
    print("TEST 2 OK: ReviewQueue — reject")


# ==================== Тесты режима review в RequestHandler ====================

async def test_review_approve_no_edit():
    handler, rq, openrouter = make_review_handler(
        response={
            "id": "cloud-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Ответ про [PERSON_1]"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    task = asyncio.create_task(handler.handle_chat_completion(make_request()))
    pending = await wait_for_pending(rq)
    request_id = pending[0]["request_id"]

    await rq.approve(request_id)  # без правок

    resp, session_id = await task

    # В облако ушли анонимизированные сообщения (не оригинал)
    sent = openrouter.captured["messages"]
    assert sent and sent[0]["content"] != "Проверь документ от Ивана Петрова"
    assert "[PERSON_1]" in sent[0]["content"], sent

    # Ответ де-анонимизирован, метаданные указывают режим review
    assert "Ивана Петрова" in resp.choices[0].message.content
    assert resp.anonymization_metadata["mode"] == "review"
    print("TEST 3 OK: approve без правок — отправлены анонимизированные сообщения")


async def test_review_approve_with_edit():
    handler, rq, openrouter = make_review_handler(
        response={
            "id": "cloud-1", "created": 1, "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    task = asyncio.create_task(handler.handle_chat_completion(make_request()))
    pending = await wait_for_pending(rq)
    request_id = pending[0]["request_id"]

    edited = "## user\nПроверь документ от Сидорова"
    await rq.approve(request_id, edited_content=edited)

    resp, session_id = await task

    # Отредактированный контент отправлен как единое user-сообщение
    sent = openrouter.captured["messages"]
    assert sent == [{"role": "user", "content": edited}], sent
    print("TEST 4 OK: approve с правками — отправлен отредактированный контент")


async def test_review_reject():
    handler, rq, openrouter = make_review_handler()
    task = asyncio.create_task(handler.handle_chat_completion(make_request()))
    pending = await wait_for_pending(rq)
    request_id = pending[0]["request_id"]

    await rq.reject(request_id, "отклонено пользователем")

    try:
        await task
        raise AssertionError("ожидался ReviewRejectedError")
    except ReviewRejectedError as e:
        assert "отклонено пользователем" in str(e)

    # В облако ничего не ушло
    assert openrouter.captured == {}, openrouter.captured
    print("TEST 5 OK: reject — облако не вызывается, ReviewRejectedError")


async def test_review_approve_stream():
    stream_chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Ответ"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    handler, rq, openrouter = make_review_handler(stream_chunks=stream_chunks)
    request = make_request(stream=True)
    prepared = await handler.prepare_chat_request(request)

    async def collect():
        out = []
        async for chunk in handler.stream_from_prepared(request, prepared):
            out.append(chunk)
        return out

    task = asyncio.create_task(collect())
    pending = await wait_for_pending(rq)
    request_id = pending[0]["request_id"]

    await rq.approve(request_id)

    chunks = await task
    sent = openrouter.captured["messages"]
    assert sent and "[PERSON_1]" in sent[0]["content"], sent
    assert any("[DONE]" in c for c in chunks), chunks
    print("TEST 6 OK: approve стрим — поток разблокирован и завершён")


async def main():
    await test_review_queue_lifecycle()
    await test_review_queue_reject()
    await test_review_approve_no_edit()
    await test_review_approve_with_edit()
    await test_review_reject()
    await test_review_approve_stream()
    print("\nALL MANUAL REVIEW TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
