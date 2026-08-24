"""
Тесты NER для локальной модели в thinking-режиме и нового контракта
(модель возвращает только text+type, оффсеты вычисляет прокси).

Проверяют:
1. Промпт не требует от модели оффсетов.
2. Извлечение JSON: чистый JSON; JSON среди «размышлений»; несколько
   JSON — берётся последний; обрезанный ответ — None.
3. Разбор ответа: JSON в content; пустой content + JSON в
   reasoning_content; нигде нет JSON — пустой список без падения.
4. Оффсеты вычисляются прокси: точное совпадение, без учёта регистра,
   ё/е; сущность, которой нет в тексте, отбрасывается.
5. extract_entities целиком: LLM-сущности из thinking + regex сливаются.

Запуск: python anonymizer_proxy\\tests\\test_ner_thinking.py (из корня проекта)
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.anonymizer.ner_service import NERService, NER_SYSTEM_PROMPT


def make_ner(message):
    """NERService с подменённым транспортом: возвращает заготовленный ответ."""
    ner = NERService()

    async def fake_request(payload):
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    ner._request_ner = fake_request
    # Пропускаем проверку доступности (иначе был бы реальный сетевой вызов)
    ner._lm_available = True
    ner._lm_checked_at = time.monotonic()
    return ner


def test_prompt_contract():
    """Промпт не требует от модели оффсетов"""
    assert '"start"' not in NER_SYSTEM_PROMPT, \
        "промпт всё ещё требует оффсеты"
    assert '"text"' in NER_SYSTEM_PROMPT and '"type"' in NER_SYSTEM_PROMPT
    print("TEST 1 OK: контракт промпта — только text+type, без оффсетов")


def test_extract_entities_json():
    ner = NERService()

    # Чистый JSON
    clean = '{"entities": [{"text": "Иван Петров", "type": "PERSON"}]}'
    assert ner._extract_entities_json(clean) == [
        {"text": "Иван Петров", "type": "PERSON"}
    ]

    # JSON среди «размышлений»: промежуточный игнорируется, берётся последний
    wrapped = (
        "Thinking Process:\n1. Analyze...\nLet me try: "
        '{"entities": [{"text": "проба", "type": "ORG"}]} — hmm, no.\n'
        "Final answer:\n"
        '{"entities": [{"text": "Иван Петров", "type": "PERSON"}, '
        '{"text": "ООО «Завод»", "type": "ORG"}]}'
    )
    got = ner._extract_entities_json(wrapped)
    assert got is not None and len(got) == 2, got
    assert got[0]["text"] == "Иван Петров"
    assert got[1]["text"] == "ООО «Завод»"

    # Пусто / нет JSON / обрезанный JSON
    assert ner._extract_entities_json("") is None
    assert ner._extract_entities_json("Thinking... no JSON") is None
    assert ner._extract_entities_json('{"entities": [{"text": "Ив') is None
    print("TEST 2 OK: извлечение JSON (чистый / среди размышлений / обрезанный)")


async def test_entities_from_content():
    """JSON в content: оффсеты вычисляются кодом прокси"""
    message = {"role": "assistant", "content": json.dumps({
        "entities": [
            {"text": "Ивана Петрова", "type": "PERSON"},
            {"text": "ооо ромашка", "type": "ORG"},  # lowercase — найдётся
        ]
    }, ensure_ascii=False), "reasoning_content": ""}
    ner = make_ner(message)
    text = "Заявление от Ивана Петрова в ООО Ромашка"

    ents, ok = await ner._llm_ner_once(text)
    assert ok, "LLM-вызов должен быть успешным"
    assert len(ents) == 2, ents

    person = [e for e in ents if e.type == "PERSON"][0]
    assert text[person.start:person.end] == "Ивана Петрова"

    org = [e for e in ents if e.type == "ORG"][0]
    # Текст берётся из оригинала (с исходным регистром)
    assert org.text == "ООО Ромашка"
    assert text[org.start:org.end] == "ООО Ромашка"
    print("TEST 3 OK: content — оффсеты от прокси (включая поиск без учёта регистра)")


async def test_entities_from_reasoning_content():
    """Пустой content + JSON в reasoning_content (thinking-режим)"""
    reasoning = (
        "Thinking Process:\n1. Analyze the text...\n"
        '{"entities": [{"text": "черновик", "type": "LOC"}]}\n'
        "2. Correcting myself: final answer\n"
        '{"entities": [{"text": "Игнатовой Веры", "type": "PERSON"}]}'
    )
    message = {"role": "assistant", "content": "",
               "reasoning_content": reasoning}
    ner = make_ner(message)
    text = "документ от Игнатовой Веры Анатольевны"

    ents, ok = await ner._llm_ner_once(text)
    assert ok, "LLM-вызов должен быть успешным"
    assert len(ents) == 1, ents
    assert ents[0].text == "Игнатовой Веры"
    assert text[ents[0].start:ents[0].end] == "Игнатовой Веры"
    print("TEST 4 OK: reasoning_content — взят последний JSON, промежуточный проигнорирован")


async def test_truncated_response_no_crash():
    """Обрезанный ответ (итогового JSON нет) — пустой список, без падения"""
    message = {"role": "assistant", "content": "",
               "reasoning_content": "Thinking Process:\n1. Analyze... (обрезано"}
    ner = make_ner(message)
    ents, ok = await ner._llm_ner_once("Любой текст")
    assert ents == [], ents
    assert not ok, "обрезанный thinking — LLM-детекция должна считаться неуспешной"
    print("TEST 5 OK: обрезанный thinking — сущностей нет, без падения")


def test_locate_entity():
    ner = NERService()
    text = "Игнатова Вера и ИГНАТОВА ВЕРА, ё-вариант: игнатёва"

    # Точное совпадение
    assert ner._locate_entity(text, "Игнатова Вера")[0] == 0
    # Без учёта регистра: текст возвращается как в оригинале
    got = ner._locate_entity(text, "игнатова вера")
    assert got is not None and text[got[0]:got[1]] == "Игнатова Вера"
    got2 = ner._locate_entity(text, "ИГНАТОВА ВЕРА")
    assert got2 is not None and text[got2[0]:got2[1]] == "ИГНАТОВА ВЕРА"
    # Нормализация ё/е
    got3 = ner._locate_entity(text, "Игнатева")
    assert got3 is not None and text[got3[0]:got3[1]] == "игнатёва"
    # Отсутствует
    assert ner._locate_entity(text, "Выдуманная Сущность") is None
    print("TEST 6 OK: _locate_entity — регистр, ё/е, отсутствие текста")


async def test_full_extract_with_regex_merge():
    """Целиком: LLM-сущности из thinking + regex-слой сливаются"""
    message = {"role": "assistant", "content": "", "reasoning_content":
               'Thinking...\n{"entities": [{"text": "Ивана Петрова", '
               '"type": "PERSON"}]}'}
    ner = make_ner(message)
    text = "Заявление от Ивана Петрова, тел. +7 916 123-45-67"

    ents, _ = await ner.extract_entities(text, use_llm=True)
    types = {e.type for e in ents}
    assert "PERSON" in types, types
    assert "PHONE" in types, types  # regex-слой
    assert "Ивана Петрова" in [e.text for e in ents]
    for e in ents:
        assert text[e.start:e.end].strip(), e
    print("TEST 7 OK: целиком — thinking-JSON и regex сливаются")


async def main():
    test_prompt_contract()
    test_extract_entities_json()
    await test_entities_from_content()
    await test_entities_from_reasoning_content()
    await test_truncated_response_no_crash()
    test_locate_entity()
    await test_full_extract_with_regex_merge()
    print("\nALL NER THINKING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

