"""
Тесты NER-движка (GLiNER): чанкование, оффсеты, маппинг меток.

Не требуют скачанной модели: используется фейковая модель.
Запуск: python anonymizer_proxy\\tests\\test_ner_engine.py (из корня проекта)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.anonymizer.gliner_engine import GlinerEngine, DEFAULT_LABEL_MAP  # noqa: E402


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
    print("\nALL NER ENGINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
