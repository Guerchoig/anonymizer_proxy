"""
Функциональные тесты исправлений по CODE_REVIEW.md
Запуск: python -m anonymizer_proxy.tests.test_fixes (из корня проекта)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import Entity, validate_session_id
from anonymizer_proxy.anonymizer.replacer import (
    TextReplacer,
    StreamDeAnonymizer,
    split_entities_by_segments,
)


async def test_segment_offsets():
    """Пункт 5: пересчёт оффсетов для нескольких сообщений"""
    seg1 = "Привет, меня зовут Иван Петров"
    seg2 = "Мой email: ivan@test.ru"
    combined = seg1 + "\n" + seg2

    # Сущность во ВТОРОМ сегменте (оффсет в склеенном тексте)
    email_start = combined.find("ivan@test.ru")
    ents = [Entity(text="ivan@test.ru", type="EMAIL", start=email_start, end=email_start + 12)]

    split = split_entities_by_segments(ents, [seg1, seg2])
    assert len(split[0]) == 0, "в первом сегменте сущностей быть не должно"
    assert len(split[1]) == 1, "сущность должна попасть во второй сегмент"
    e = split[1][0]
    assert seg2[e.start:e.end] == "ivan@test.ru", "локальные оффсеты неверны"
    print("TEST 1 OK: пересчёт оффсетов мультисообщений")

    # Анонимизация второго сообщения с локальными оффсетами
    replacer = TextReplacer()

    async def add_map(v, t):
        return f"[{t}_1]"

    anon, maps = await replacer.anonymize(seg2, split[1], add_map)
    assert anon == "Мой email: [EMAIL_1]", f"получено: {anon}"
    print("TEST 2 OK: анонимизация второго сообщения")


async def test_safe_deanonymize():
    """Пункты 8-9: безопасная де-анонимизация в один проход"""
    replacer = TextReplacer()

    # Значение с regex-спецсимволами замены (\g<0>) не должно ломать замену
    mappings = {"[PERSON_1]": "Иван \\g<0> Петров", "[ORG_1]": "ООО Ромашка"}
    text = "[PERSON_1] работает в [ORG_1], а [ORG_1] платит [PERSON_1]"
    res = await replacer.deanonymize(text, mappings)
    expected = "Иван \\g<0> Петров работает в ООО Ромашка, а ООО Ромашка платит Иван \\g<0> Петров"
    assert res == expected, f"получено: {res}"

    # Каскадная замена: значение содержит другой токен — один проход не каскадит
    mappings2 = {"[A_1]": "значение с [B_1]", "[B_1]": "ДРУГОЕ"}
    res2 = await replacer.deanonymize("[A_1]", mappings2)
    assert res2 == "значение с [B_1]", f"каскадная замена! получено: {res2}"
    print("TEST 3 OK: безопасная де-анонимизация (один проход, без каскадов)")


async def test_stream_buffering():
    """Пункт 7: буферизация разорванных токенов при стриминге"""
    replacer = TextReplacer()
    deano = StreamDeAnonymizer(replacer, {"[PERSON_1]": "Иван Петров"})

    out1 = await deano.feed("Привет, [PER")
    out2 = await deano.feed("SON_1]!")
    tail = await deano.flush()
    full = out1 + out2 + tail
    assert full == "Привет, Иван Петров!", f"получено: {full!r}"
    assert "[" not in full, "токен утечёт клиенту"
    print("TEST 4 OK: буферизация разорванного токена")

    # Токен целиком в одном чанке
    deano2 = StreamDeAnonymizer(replacer, {"[PERSON_1]": "Иван Петров"})
    out = await deano2.feed("Привет, [PERSON_1]!")
    tail2 = await deano2.flush()
    assert out + tail2 == "Привет, Иван Петров!", f"получено: {out + tail2!r}"
    print("TEST 5 OK: токен целиком в одном чанке")


def test_session_id_validation():
    """Пункт 4: валидация session_id (path traversal)"""
    assert validate_session_id("abc-123_XYZ") == "abc-123_XYZ"
    assert validate_session_id(None) is None
    for bad in ["../../etc/passwd", "a/b", "a\\b", "x" * 65, "a b", "a;b"]:
        try:
            validate_session_id(bad)
            raise AssertionError(f"должен был отклонить: {bad!r}")
        except ValueError:
            pass
    print("TEST 6 OK: валидация session_id")


def test_llm_offset_validation():
    """Пункт 6: валидация оффсетов от LLM"""
    from anonymizer_proxy.anonymizer.ner_service import NERService

    ner = NERService()
    text = "Иван Петров работает в ООО Ромашка"

    # Корректные оффсеты
    ok = ner._validate_entity_offsets(text, "Иван Петров", 0, 11)
    assert ok == (0, 11), f"получено: {ok}"

    # Неверные оффсеты, но текст находится — восстанавливаем через find
    fixed = ner._validate_entity_offsets(text, "ООО Ромашка", 5, 10)
    assert fixed is not None
    assert text[fixed[0]:fixed[1]] == "ООО Ромашка"

    # Текст отсутствует — отбрасываем
    none = ner._validate_entity_offsets(text, "Выдуманная Сущность", 0, 10)
    assert none is None
    print("TEST 7 OK: валидация оффсетов от LLM")


def test_cross_segment_entity_split():
    """Регрессия 2026-09-08: сущность, пересекающая границу сегментов
    (в склеенном тексте содержит «\n»), режется на части В КАЖДЫЙ
    пересекаемый сегмент — иначе PII в «меньшей» части утекает."""
    seg1 = "Sasha"
    seg2 = "А.В. Гершойг"
    combined = seg1 + "\n" + seg2
    ent = Entity(text=combined, type="PERSON",
                 start=0, end=len(combined), confidence=0.9)

    split = split_entities_by_segments([ent], [seg1, seg2])

    assert len(split[0]) == 1, split
    assert split[0][0].text == "Sasha", split[0]
    assert seg1[split[0][0].start:split[0][0].end] == "Sasha"
    assert len(split[1]) == 1, split
    assert split[1][0].text == "А.В. Гершойг", split[1]
    assert seg2[split[1][0].start:split[1][0].end] == "А.В. Гершойг"
    print("TEST 8 OK: кросс-сегментная сущность режется во все сегменты")


async def main():
    await test_segment_offsets()
    await test_safe_deanonymize()
    await test_stream_buffering()
    test_session_id_validation()
    test_llm_offset_validation()
    test_cross_segment_entity_split()
    print("\nALL FUNCTIONAL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())