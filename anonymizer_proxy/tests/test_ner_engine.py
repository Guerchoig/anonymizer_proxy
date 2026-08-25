"""
Тесты NER-движка (GLiNER): чанкование, оффсеты, маппинг меток,
автодетекция ONNX-провайдера, фоллбек ONNX→PyTorch, таймаут инференса.

Не требуют скачанной модели: используется фейковая модель.
Запуск: python anonymizer_proxy\\tests\\test_ner_engine.py (из корня проекта)
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.anonymizer.gliner_engine import (  # noqa: E402
    GlinerEngine, DEFAULT_LABEL_MAP, detect_onnx_providers,
)
from anonymizer_proxy.config import NER_ENGINE  # noqa: E402


class _Cfg:
    max_len = 512


class FakeModel:
    config = _Cfg()

    def __init__(self, response):
        self._response = response

    def predict_entities(self, text, labels, flat_ner=True, threshold=0.5, **kw):
        return self._response(text, labels)


def make_engine(response_fn, safe_chunk_chars=100):
    eng = GlinerEngine(model_name="fake/model", threshold=0.5, chunk_overlap_chars=40)
    eng._model = FakeModel(response_fn)
    eng._loaded = True
    eng._safe_chunk_chars = safe_chunk_chars
    return eng


def test_split_into_chunks_short():
    eng = make_engine(lambda t, l: [])
    text = "короткий текст"
    assert eng._split_into_chunks(text) == [(text, 0)]
    print("TEST 1 OK: короткий текст — один чанк")


def test_split_into_chunks_long():
    eng = make_engine(lambda t, l: [], safe_chunk_chars=100)
    text = "блок данных\n" * 60  # длинный текст
    chunks = eng._split_into_chunks(text)
    assert len(chunks) > 1, f"ожидалось несколько чанков: {chunks}"
    for chunk_text, offset in chunks:
        assert len(chunk_text) <= 100, f"чанк длиннее лимита: {len(chunk_text)}"
        assert text[offset:offset + len(chunk_text)] == chunk_text
    assert chunks[0][1] == 0
    last_text, last_off = chunks[-1]
    assert last_off + len(last_text) == len(text), "хвост текста потерян"
    for (t1, o1), (t2, o2) in zip(chunks, chunks[1:]):
        assert o2 > o1, "чанки не продвигаются вперёд"
        assert o2 < o1 + len(t1), "дыра между чанками"
    print(f"TEST 2 OK: чанкование длинного текста ({len(chunks)} чанков)")


async def test_predict_offsets_and_mapping():
    def response(text, labels):
        return [{"start": 0, "end": 6, "text": "Иванов", "label": "person name", "score": 0.9}]

    eng = make_engine(response)
    entities = await eng.predict("Иванов работает", ["PERSON"])
    assert len(entities) == 1, entities
    e = entities[0]
    assert e.type == "PERSON" and e.text == "Иванов"
    assert e.start == 0 and e.end == 6
    print("TEST 3 OK: оффсеты и маппинг меток")


async def test_predict_chunk_offset_shift():
    # Сущность попадает во второй чанк — оффсет должен сдвинуться
    def response(text, labels):
        if "Иванов" in text:
            i = text.find("Иванов")
            return [{"start": i, "end": i + 6, "text": "Иванов", "label": "person name", "score": 0.9}]
        return []

    eng = make_engine(response, safe_chunk_chars=100)
    text = "z" * 150 + "Иванов"
    entities = await eng.predict(text, ["PERSON"])
    found = [e for e in entities if e.type == "PERSON"]
    assert len(found) == 1, found
    assert text[found[0].start:found[0].end] == "Иванов"
    print("TEST 4 OK: перенос оффсетов между чанками")


async def test_predict_dedup_in_overlap():
    # Одна и та же сущность в зоне перекрытия не дублируется
    def response(text, labels):
        if "Иванов" in text:
            return [{"start": text.find("Иванов"), "end": text.find("Иванов") + 6,
                     "text": "Иванов", "label": "person name", "score": 0.9}]
        return []

    eng = make_engine(response, safe_chunk_chars=100)
    # Сущность на границе первого и второго чанков
    text = "x" * 90 + "Иванов" + "y" * 90
    entities = await eng.predict(text, ["PERSON"])
    found = [e for e in entities if e.type == "PERSON"]
    assert len(found) == 1, f"дубликат в зоне перекрытия: {found}"
    print("TEST 5 OK: дедупликация в зоне перекрытия")


async def main():
    test_split_into_chunks_short()
    test_split_into_chunks_long()
    await test_predict_offsets_and_mapping()
    await test_predict_chunk_offset_shift()
    await test_predict_dedup_in_overlap()
    test_detect_onnx_providers()
    test_backend_fallback_onnx_to_torch()
    test_backend_torch_skips_onnx()
    await test_predict_timeout()
    print("\nALL NER ENGINE TESTS PASSED")


def test_detect_onnx_providers():
    """Автодетекция всегда даёт список с CPU-фоллбеком в конце"""
    providers = detect_onnx_providers()
    if not providers:
        # onnxruntime не установлен — легитимный случай (движок уйдёт в torch)
        print("TEST 6 OK: onnxruntime не установлен (провайдеры пустые)")
        return
    assert providers[-1] == "CPUExecutionProvider", providers
    # GPU-провайдер не более одного и идёт первым
    gpu = [p for p in providers if p in ("CUDAExecutionProvider", "DmlExecutionProvider")]
    assert len(gpu) <= 1, providers
    if gpu:
        assert providers[0] == gpu[0], providers
    print(f"TEST 6 OK: детекция провайдеров -> {providers}")


def test_backend_fallback_onnx_to_torch():
    """NER_BACKEND=auto: сбой ONNX -> видимый откат на PyTorch"""
    eng = GlinerEngine(model_name="fake/model", chunk_overlap_chars=40)
    calls = {"onnx": 0, "torch": 0}

    def _fail_onnx():
        calls["onnx"] += 1
        raise RuntimeError("ONNX недоступен")

    def _fake_torch():
        calls["torch"] += 1
        eng._model = FakeModel(lambda t, l: [])
        eng._backend = "torch"
        eng._providers = []
        eng._finalize_load()

    eng._load_onnx = _fail_onnx
    eng._load_torch = _fake_torch
    saved_backend = NER_ENGINE.get("backend")
    try:
        NER_ENGINE["backend"] = "auto"
        eng._ensure_loaded()
    finally:
        NER_ENGINE["backend"] = saved_backend or "auto"

    assert calls == {"onnx": 1, "torch": 1}, calls
    assert eng._backend == "torch" and eng.is_available()
    assert "torch" in eng.describe()
    print("TEST 7 OK: фоллбек ONNX -> PyTorch")


def test_backend_torch_skips_onnx():
    """NER_BACKEND=torch: ONNX даже не пробуется"""
    eng = GlinerEngine(model_name="fake/model", chunk_overlap_chars=40)
    called = {"onnx": False}

    def _fail_onnx():
        called["onnx"] = True
        raise AssertionError("ONNX не должен вызываться при backend=torch")

    def _fake_torch():
        eng._model = FakeModel(lambda t, l: [])
        eng._backend = "torch"
        eng._providers = []
        eng._finalize_load()

    eng._load_onnx = _fail_onnx
    eng._load_torch = _fake_torch
    saved_backend = NER_ENGINE.get("backend")
    try:
        NER_ENGINE["backend"] = "torch"
        eng._ensure_loaded()
    finally:
        NER_ENGINE["backend"] = saved_backend or "auto"

    assert not called["onnx"]
    assert eng._backend == "torch"
    print("TEST 8 OK: backend=torch пропускает ONNX")


async def test_predict_timeout():
    """Таймаут NER_TIMEOUT_SECONDS прерывает зависший инференс"""
    class _SlowModel(FakeModel):
        def predict_entities(self, text, labels, flat_ner=True, threshold=0.5, **kw):
            time.sleep(1.0)
            return []

    eng = GlinerEngine(
        model_name="fake/model", chunk_overlap_chars=40, timeout_seconds=0.2,
    )
    eng._model = _SlowModel(lambda t, l: [])
    eng._loaded = True
    eng._safe_chunk_chars = 100

    try:
        await eng.predict("текст", ["PERSON"])
        raise AssertionError("ожидался TimeoutError")
    except TimeoutError:
        pass
    print("TEST 9 OK: таймаут инференса")


if __name__ == "__main__":
    asyncio.run(main())
