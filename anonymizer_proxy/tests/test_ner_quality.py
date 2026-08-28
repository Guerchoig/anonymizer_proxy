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


def test_expand_acronym_word_boundaries():
    """Акроним расширяется только по границам слова и в исходном регистре.

    Регрессия: значение «ИС» раньше подставлялось внутрь любых слов,
    содержащих слог «ис» («рисками», «подпись», «система»), независимо
    от регистра букв.
    """
    ner = NERService()
    text = (
        "Управление рисками требует сервиса и контроля.\n"
        "Подпись исполнителя: система испытаний.\n"
        "Диспетчеризация, регистрация, классификация.\n"
        "Автономное строчное слово ис не акроним.\n"
        "Целевая ИС построена на базе платформы.\n"
        "См. раздел ИС: требования."
    )
    first = text.find("ИС построена")
    ents = [Entity(text="ИС", type="ORG", start=first, end=first + 2)]

    expanded = ner._expand_all_occurrences(text, ents)

    assert len(expanded) == 2, \
        f"ожидалось 2 автономных вхождения «ИС», получено {len(expanded)}: " \
        f"{[text[e.start:e.end] for e in expanded]}"
    for e in expanded:
        assert text[e.start:e.end] == "ИС", \
            f"внутрисловное/строчное вхождение заменено: {text[e.start:e.end]!r}"
    # Буквы «ис» внутри слов остались нетронутыми
    for word in ("рисками", "сервиса", "Подпись", "испытаний",
                 "Диспетчеризация", "регистрация", "слово ис"):
        idx = text.find(word)
        assert idx != -1 and text[idx:idx + len(word)] == word
        span_lo, span_hi = idx, idx + len(word)
        assert not any(e.start < span_hi and e.end > span_lo for e in expanded), \
            f"задето слово: {word}"
    print("TEST 5.1 OK: акроним — только автономные вхождения в своём регистре")


def test_expand_case_insensitive_names_still_work():
    """Имена/названия (не акронимы) по-прежнему ищутся без учёта регистра."""
    ner = NERService()
    text = "Компания Иван Петров подписала акт. Исполнитель — иван петров."
    first = text.find("Иван Петров")
    ents = [Entity(text="Иван Петров", type="PERSON", start=first,
                   end=first + len("Иван Петров"))]

    expanded = ner._expand_all_occurrences(text, ents)

    assert len(expanded) == 2, \
        f"ожидалось 2 вхождения (регистр не важен), получено {len(expanded)}"
    values = [text[e.start:e.end].lower() for e in expanded]
    assert all(v == "иван петров" for v in values)
    print("TEST 5.2 OK: неакронимы расширяются без учёта регистра")


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


def test_regex_web_addresses():
    """Regex-детектор WEB: URL, www, «голые» домены по TLD; email цел"""
    ner = NERService()
    text = (
        "Подробнее на сайте www.iris-retail.ru, раздел поставок. "
        "Портал: https://portal.example-iris.ru/docs?id=42&page=1. "
        "Наш домен iris-retail.ru обслуживает клиентов. "
        "Пишите на info@iris-retail.ru или sales@example.com. "
        "Кириллический домен: пример.рф. "
        "Отчёт report.docx и версия python 3.14 не адреса сайтов. "
    )
    entities = ner._extract_regex_entities(text)

    def spans(etype):
        return [text[e.start:e.end] for e in entities if e.type == etype]

    web = spans("WEB")
    emails = spans("EMAIL")

    # Полный URL с путём и query — одним спаном
    assert "https://portal.example-iris.ru/docs?id=42&page=1" in web, web
    # www с хвостовой запятой — без пунктуации
    assert "www.iris-retail.ru" in web, web
    assert not any(v.endswith(",") or v.endswith(".") for v in web), web
    # Голый домен по TLD
    assert "iris-retail.ru" in web, web
    # Кириллический .рф
    assert "пример.рф" in web, web
    # Email не разбит на домен+WEB: два целых адреса, и нет WEB-спанов внутри
    assert "info@iris-retail.ru" in emails, emails
    assert "sales@example.com" in emails, emails
    for e in entities:
        if e.type == "WEB":
            frag = text[e.start:e.end]
            assert "@" not in frag, f"WEB накрыл email: {frag}"

    # Негативы: файлы, версии, «слово.слово» без TLD не адреса сайтов
    joined = " ".join(web)
    assert "report.docx" not in joined, web
    assert "3.14" not in joined, web
    print(f"TEST 11 OK: regex-детектор WEB ({len(web)} адресов, email целы)")


async def test_web_roundtrip_replacement():
    """Полный цикл: URL/домены маскируются [WEB_N] и восстанавливаются"""
    ner = NERService()
    text = (
        "Сайт компании www.iris-retail.ru и портал "
        "https://portal.example.ru/docs?id=1. Пишите на info@iris-retail.ru."
    )
    entities, _ = await ner.extract_entities(text, use_llm=False)

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
    # deanonymize ждёт словарь {токен: значение}
    mapping = {tok: val for val, tok in tokens.items()}
    assert "www.iris-retail.ru" not in anon, anon
    assert "portal.example.ru" not in anon, anon
    assert "[WEB_1]" in anon and "[WEB_2]" in anon, anon
    # Email остался целым плейсхолдером, не дробился на WEB
    assert "[EMAIL_1]" in anon, anon
    assert anon.count("[WEB_") + anon.count("[EMAIL_") >= 3, anon

    # Round-trip: всё восстановлено дословно
    deanon = await replacer.deanonymize(anon, mapping)
    assert deanon == text, deanon
    print("TEST 12 OK: WEB round-trip — маскировка и восстановление")


def test_money_without_currency_keyword():
    """Суммы без слова валюты (валюта в шапке таблицы) маскируются;
    почтовые индексы и годы — нет"""
    ner = NERService()
    text = (
        "Этапы работ и затраты, руб., без НДС:\n"
        "Настройка модуля ⏎ 45 дней ⏎ 7 432 480,00\n"
        "Доработка отчётов ⏎ 2 дней ⏎ 83 600,00\n"
        "Поддержка ⏎ 39 дней ⏎ 5 186 720,00 руб\n"
        "Бюджет проекта 1,5 млн рублей, НДС 20%.\n"
        "Адрес: 101000, г. Москва, а/я 25, индекс 614087.\n"
    )
    entities = ner._extract_regex_entities(text)

    def spans(etype):
        return [text[e.start:e.end] for e in entities if e.type == etype]

    money = spans("MONEY")
    # Суммы в ячейках таблицы без слова валюты
    for expected in ("7 432 480,00", "83 600,00",
                     "5 186 720,00 руб", "1,5 млн рублей"):
        assert expected in money, f"{expected!r} не найдено: {money}"
    # Почтовые индексы и прочие не-суммы не замаскированы
    for forbidden in ("101000", "614087"):
        assert not any(forbidden in v for v in money), \
            f"{forbidden} ложно замаскирован: {money}"
    print(f"TEST 13 OK: MONEY без валюты — {len(money)} сумм, индексы целы")


def test_replace_by_value_boundaries():
    """replace_by_value: короткое значение не портит цифры в датах, суммах
    и уже вставленных токенах (регрессия вложенных плейсхолдеров)"""
    import re as _re

    r = TextReplacer()
    text = (
        "Редакция 3, дата 31.07.2023, сумма [MONEY_63] и срок 13 дней. "
        "Контрагент: АО «НПФ «БЛАГОСОСТОЯНИЕ»."
    )
    value_to_token = {
        "3": "[ORG_120]",
        "АО «НПФ «БЛАГОСОСТОЯНИЕ»": "[ORG_2]",
    }
    out = r.replace_by_value(text, value_to_token)

    # Автономная '3' заменена
    assert "Редакция [ORG_120]," in out, out
    # Цифры внутри дат/сроков/токенов НЕ тронуты
    assert "31.07.2023" in out, out
    assert "13 дней" in out, out
    assert "[MONEY_63]" in out, out
    # Длинное значение заменено целиком
    assert "[ORG_2]" in out and "БЛАГОСОСТОЯНИЕ" not in out, out
    # Вложенных токенов не появилось
    assert not _re.search(r"\[[A-Z_]+_\d+\[", out), out
    print("TEST 14 OK: replace_by_value — границы слов защищают даты и токены")


async def main():
    test_split_into_chunks_short()
    test_split_into_chunks_long()
    test_expand_all_occurrences()
    test_expand_long_value_priority()
    test_expand_short_values_not_expanded()
    test_expand_acronym_word_boundaries()
    test_expand_case_insensitive_names_still_work()
    test_regex_org_forms()
    await test_full_pipeline_all_occurrences_replaced()
    test_quoted_core_expansion()
    test_homoglyph_expansion()
    await test_entities_cache_hit()
    test_regex_web_addresses()
    await test_web_roundtrip_replacement()
    test_money_without_currency_keyword()
    test_replace_by_value_boundaries()
    print("\nALL NER QUALITY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())