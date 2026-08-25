"""
Тесты повышения качества анонимизации:
- чанкование длинного текста вместо обрезки (NER_MAX_INPUT_CHARS);
- расширение всех вхождений найденной сущности;
- regex-детектор оргформ (ООО/АО/НПФ/Фонд + название).

Запуск: python anonymizer_proxy\\tests\\test_ner_quality.py (из корня проекта)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anonymizer_proxy.models.schemas import Entity
from anonymizer_proxy.anonymizer.ner_service import NERService
from anonymizer_proxy.anonymizer.gliner_engine import GlinerEngine
from anonymizer_proxy.anonymizer.replacer import TextReplacer


class _FakeGlinerModel:
    """Фейковая модель GLiNER: возвращает заданные сущности"""

    config = type("C", (), {"max_len": 512})

    def __init__(self, found):
        self.found = found  # {значение: тип}

    def predict_entities(self, text, labels, flat_ner=True, threshold=0.5, **kw):
        ents = []
        for value, etype in self.found.items():
            start = 0
            while True:
                idx = text.find(value, start)
                if idx == -1:
                    break
                ents.append({
                    "start": idx, "end": idx + len(value),
                    "text": value, "label": etype, "score": 0.9,
                })
                start = idx + len(value)
        return ents


def test_split_into_chunks_short():
    """Текст короче лимита идёт одним чанком без изменений"""
    eng = GlinerEngine(model_name="fake/model", chunk_overlap_chars=200)
    eng._safe_chunk_chars = 1000
    text = "Короткий текст для NER"
    chunks = eng._split_into_chunks(text)
    assert chunks == [(text, 0)], f"получено: {chunks}"
    print("TEST 1 OK: короткий текст — один чанк")


def test_split_into_chunks_long():
    """Длинный текст режется на чанки с перекрытием и без потерь"""
    eng = GlinerEngine(model_name="fake/model", chunk_overlap_chars=200)
    eng._safe_chunk_chars = 1000
    # ~2000+ символов из коротких строк (как строки таблицы документа)
    text = "".join(f"строка {i:04d} тестовые данные\n" for i in range(80))

    chunks = eng._split_into_chunks(text)

    assert len(chunks) > 1, "длинный текст должен быть разбит на чанки"
    for chunk_text, offset in chunks:
        assert len(chunk_text) <= 1000, "чанк длиннее лимита"
        assert text[offset:offset + len(chunk_text)] == chunk_text, \
            "чанк не совпадает со срезом исходного текста"
    # Первый чанк начинается с нуля, последний заканчивается в конце текста
    assert chunks[0][1] == 0
    last_text, last_off = chunks[-1]
    assert last_off + len(last_text) == len(text), "хвост текста потерян"
    # Соседние чанки перекрываются не больше чем на chunk_overlap_chars,
    # и идут только вперёд
    for (t1, o1), (t2, o2) in zip(chunks, chunks[1:]):
        assert o2 > o1, "чанки не продвигаются вперёд"
        assert o1 + len(t1) - o2 <= 200, "перекрытие больше chunk_overlap_chars"
        assert o2 < o1 + len(t1), "дыра между чанками"
    # Границы выравниваются по переводам строк (кроме последнего чанка)
    for chunk_text, _ in chunks[:-1]:
        assert chunk_text.endswith("\n"), "граница чанка не по строке"
    print(f"TEST 2 OK: чанкование длинного текста ({len(chunks)} чанков)")


def test_expand_all_occurrences():
    """Найденная один раз сущность расширяется на все вхождения"""
    ner = NERService()
    text = (
        "Компания Ирис выпустила продукт. "
        "Ирис планирует расширение. "
        "Приложение 3. О компании Ирис."
    )
    first = text.find("Ирис")
    ents = [Entity(text="Ирис", type="ORG", start=first, end=first + 4)]

    expanded = ner._expand_all_occurrences(text, ents)

    assert len(expanded) == 3, f"ожидалось 3 вхождения, получено {len(expanded)}"
    for e in expanded:
        assert text[e.start:e.end] == "Ирис", "оффсеты вхождения неверны"
    # Вхождения отсортированы по позиции
    assert [e.start for e in expanded] == sorted(e.start for e in expanded)
    print("TEST 3 OK: расширение на все вхождения")


def test_expand_long_value_priority():
    """Длинные значения в приоритете: короткие внутри них не дублируются"""
    ner = NERService()
    text = (
        "Внедрение СЭД в АО НПФ Благосостояние выполнено. "
        "АО НПФ Благосостояние — заказчик. "
        "Благосостояние — фонд."
    )
    long_value = "АО НПФ Благосостояние"
    short_value = "Благосостояние"
    first_long = text.find(long_value)
    # Короткое значение указано с позицией ВНУТРИ длинного (как бывает от LLM)
    inner_short = first_long + len("АО НПФ ")
    ents = [
        Entity(text=long_value, type="ORG",
               start=first_long, end=first_long + len(long_value)),
        Entity(text=short_value, type="ORG",
               start=inner_short, end=inner_short + len(short_value)),
    ]

    expanded = ner._expand_all_occurrences(text, ents)

    long_spans = [e for e in expanded if e.text == long_value]
    short_spans = [e for e in expanded if e.text == short_value]
    assert len(long_spans) == 2, "оба вхождения длинной фразы должны быть заменены"
    assert len(short_spans) == 1, \
        "самостоятельное вхождение короткого имени должно остаться одно"
    print("TEST 4 OK: приоритет длинных значений при расширении")


def test_expand_short_values_not_expanded():
    """Односимвольные значения не расширяются на весь текст"""
    ner = NERService()
    text = "Я Я Я"
    ents = [Entity(text="Я", type="PERSON", start=0, end=1)]
    expanded = ner._expand_all_occurrences(text, ents)
    assert len(expanded) == 1, "односимвольное значение не должно расширяться"
    assert (expanded[0].start, expanded[0].end) == (0, 1)
    print("TEST 5 OK: короткие значения не расширяются")


def test_regex_org_forms():
    """Regex-детектор оргформ: ловит юрлица, не ловит бытовые фразы"""
    ner = NERService()
    text = (
        "Внедрение СЭД в АО НПФ Благосостояние выполнило ООО «Ромашка». "
        "Договор с АО «НПФ «БЛАГОСОСТОЯНИЕ» подписан. "
        "Это фонд оплаты труда и банк данных, а не организации."
    )
    entities = ner._extract_regex_entities(text)
    org_texts = {e.text for e in entities if e.type == "ORG"}

    assert "АО НПФ Благосостояние" in org_texts, f"не найдено: {org_texts}"
    assert "ООО «Ромашка»" in org_texts, f"не найдено: {org_texts}"
    assert "АО «НПФ «БЛАГОСОСТОЯНИЕ»" in org_texts, f"не найдено: {org_texts}"
    # Бытовые фразы в нижнем регистре не должны ловиться
    assert not any("фонд оплаты труда" in t for t in org_texts), \
        f"ложное срабатывание: {org_texts}"
    assert not any("банк данных" in t for t in org_texts), \
        f"ложное срабатывание: {org_texts}"
    print(f"TEST 6 OK: regex-детектор оргформ ({len(org_texts)} сущностей)")


async def test_full_pipeline_all_occurrences_replaced():
    """
    Полный цикл без LLM: extract_entities (regex + расширение) → замена.
    Все вхождения PII заменены, одно значение — один плейсхолдер.
    """
    ner = NERService()
    text = (
        "КП для АО НПФ Благосостояние.\n"
        "Стоимость: 100 000,00 рублей. Итого 100 000,00 рублей без учёта НДС.\n"
        "Проект: Внедрение СЭД в АО НПФ Благосостояние.\n"
    )

    entities, _ = await ner.extract_entities(text, use_llm=False)

    org_spans = [(e.start, e.end) for e in entities if e.type == "ORG"]
    assert len(org_spans) == 2, \
        f"ожидалось 2 вхождения оргформы, получено {len(org_spans)}"
    for s, e in org_spans:
        assert text[s:e] == "АО НПФ Благосостояние"
    money = [e for e in entities if e.type == "MONEY"]
    assert len(money) == 2, f"ожидалось 2 суммы, получено {len(money)}"

    replacer = TextReplacer()
    tokens: dict[str, str] = {}
    counters: dict[str, int] = {}

    async def add_map(value: str, etype: str) -> str:
        if value in tokens:
            return tokens[value]
        counters[etype] = counters.get(etype, 0) + 1
        token = f"[{etype}_{counters[etype]}]"
        tokens[value] = token
        return token

    anon, _ = await replacer.anonymize(text, entities, add_map)
    assert "Благосостояние" not in anon, f"имя утекло: {anon}"
    assert "100 000,00 рублей" not in anon, f"сумма утекла: {anon}"
    assert anon.count("[ORG_1]") == 2, "все вхождения должны получить один токен"
    print("TEST 7 OK: полный цикл — все вхождения заменены одним плейсхолдером")


def test_quoted_core_expansion():
    """Оргформа с названием в кавычках покрывает и короткие вхождения названия"""
    ner = NERService()
    text = (
        "Благодарим за обращение в ООО «ИРИС» по вопросу оказания услуг.\n"
        "Приложение 3. О компании ИРИС.\n"
        "Компания «ИРИС» – ведущий поставщик.\n"
        "Преимущества работы с ИРИС\n"
    )
    full = "ООО «ИРИС»"
    first = text.find(full)
    ents = [Entity(text=full, type="ORG", start=first, end=first + len(full))]

    # Ядро названия добавляется к сущностям
    ents = ner._derive_quoted_cores(text, ents)
    cores = [e.text for e in ents]
    assert "ИРИС" in cores, f"ядро не добавлено: {cores}"

    # Расширение покрывает все вхождения: полную оргформу и 3 коротких
    expanded = ner._expand_all_occurrences(text, ents)
    spans = [(e.start, e.end) for e in expanded if e.type == "ORG"]
    assert len(spans) == 4, \
        f"ожидалось 4 вхождения, получено {len(spans)}: {spans}"
    for s, e in spans:
        assert text[s:e] in (full, "ИРИС"), text[s:e]
    # ИРИС внутри ООО «ИРИС» не дублируется отдельным спаном
    inner = first + full.find("ИРИС")
    assert (inner, inner + 4) not in spans
    print("TEST 8 OK: ядро названия в кавычках покрывает короткие вхождения")


def test_homoglyph_expansion():
    """Омоглифы кириллица/латиница: «1С» и «1C» покрываются одним значением"""
    ner = NERService()
    text = (
        "Крупнейшие интеграторы решений 1С.\n"
        "Платформа 1C и продукты 1С:Предприятие.\n"
    )
    # Модель вернула латиницу — должны покрыться и кириллические вхождения
    first = text.find("1C")
    ents = [Entity(text="1C", type="ORG", start=first, end=first + 2)]
    expanded = ner._expand_all_occurrences(text, ents)
    spans = [(e.start, e.end) for e in expanded]
    assert len(spans) == 3, \
        f"ожидалось 3 вхождения (1С, 1C, 1С), получено {spans}"
    for s, e in spans:
        assert text[s:e] in ("1С", "1C"), text[s:e]
    # _locate_entity находит кириллицу по латинскому значению и наоборот
    got = ner._locate_entity("решений 1С", "1C")
    assert got is not None and got[2] == "1С"
    got2 = ner._locate_entity("platform 1C", "1С")
    assert got2 is not None and got2[2] == "1C"
    print("TEST 9 OK: омоглифы кириллица/латиница покрываются одним значением")


async def test_entities_cache_hit():
    """Повторный NER того же текста берётся из кэша, без инференса"""
    from anonymizer_proxy.models.schemas import Entity as _Entity

    class _CountingEngine:
        """Движок-счётчик: фиксирует вызовы инференса"""

        def __init__(self):
            self.calls = 0

        async def predict(self, text, categories):
            self.calls += 1
            idx = text.find("Иванов")
            return [_Entity(text="Иванов", type="PERSON",
                            start=idx, end=idx + 6, confidence=0.9)]

    ner = NERService()
    ner._engine = _CountingEngine()

    text = "Документ подготовлен Ивановым для ООО «Ромашка»"
    ents1, _, failed1 = await ner.extract_entities_detailed(text, use_llm=True)
    ents2, _, failed2 = await ner.extract_entities_detailed(text, use_llm=True)

    assert not failed1 and not failed2
    assert ner._engine.calls == 1, f"инференс выполнен {ner._engine.calls} раз вместо 1"
    assert len(ents1) > 0 and len(ents2) == len(ents1), "кэш вернул другой результат"
    # Изменение текста — мимо кэша
    await ner.extract_entities_detailed(text + " (версия 2)", use_llm=True)
    assert ner._engine.calls == 2, "новый текст должен пройти мимо кэша"
    print("TEST 10 OK: кэш результатов NER по тексту")


async def main():
    test_split_into_chunks_short()
    test_split_into_chunks_long()
    test_expand_all_occurrences()
    test_expand_long_value_priority()
    test_expand_short_values_not_expanded()
    test_regex_org_forms()
    await test_full_pipeline_all_occurrences_replaced()
    test_quoted_core_expansion()
    test_homoglyph_expansion()
    await test_entities_cache_hit()
    print("\nALL NER QUALITY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())