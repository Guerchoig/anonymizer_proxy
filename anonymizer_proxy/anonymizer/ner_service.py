"""
NER-сервис для извлечения именованных сущностей.

Использует локальный GLiNER-движок (in-process) как основной метод
и regex-паттерны как быстрый детерминированный слой. Оффсеты сущностей
GLiNER возвращает сам; для устойчивости сохранён поиск без учёта
регистра и различия ё/е.
"""
import hashlib
import logging
import re
import time
from typing import Optional

from ..config import NER_ENGINE, PII_CATEGORIES
from .gliner_engine import GlinerEngine
from .natasha_engine import NatashaEngine

logger = logging.getLogger("anonymizer_proxy.ner")


class NERUnavailableError(ValueError):
    """Локальная NER-модель не загрузилась или не вернула результат.

    Отличает «NER упал» от «NER отработал, но PII не нашёл»: в первом случае
    анонимизацию нельзя считать выполненной, и поток обязан сообщить об ошибке,
    а не молча выдать «0 сущностей».
    """

# Regex-паттерны для быстрого детекта структурированных данных
REGEX_PATTERNS = {
    "PASSPORT": [
        # Российский паспорт: серия (4 цифры) + номер (6 цифр)
        r"\b(\d{4}\s*\d{6})\b",
        # С указанием "серия", "номер"
        r"(?:серия|паспорт)\s*[:\s]*\s*(\d{4})\s*(?:№|номер)?\s*[:\s]*\s*(\d{6})",
    ],
    "PHONE": [
        # Российские телефоны
        r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"8\s*\d{3}\s*\d{3}\s*\d{2}\s*\d{2}",
    ],
    "PERSON": [
        # Русские ФИО с инициалами — детерминированная страховка: модель
        # пропускала «Фамилия И.О.» в подписях документов (скор ниже порога).
        # Записи кортежем (паттерн, флаги): 0 = с учётом регистра, чтобы
        # не ловить обычные слова и аббревиатуры.
        # «Вдовин Д.В.» / «Вдовин Д. В.» (фамилия перед инициалами)
        (
            r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.",
            0,
        ),
        # «Д.В. Вдовин» / «Д. В. Вдовин» (инициалы перед фамилией).
        # Разделитель перед фамилией — только пробел/табуляция ([ \t]), не
        # \s: иначе «Д.В.\nИсполнитель» матчится как «инициалы+фамилия» и
        # длинным ложным спаном перебивает точное «Фамилия И.О.»
        (
            r"\b[А-ЯЁ]\.[ \t]?[А-ЯЁ]\.[ \t]+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?",
            0,
        ),
    ],
    "EMAIL": [
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    ],
    "WEB": [
        # Полный URL с протоколом (путь и query входят в спан)
        r"https?://[^\s<>\"')\]]+",
        # www-домены с опциональным путём
        r"\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[^\s<>\"')\]]*)?",
        # «Голый» домен по белому списку TLD (включая кириллический .рф).
        # (?<![\w@.-]) — не продолжение слова и не доменная часть email:
        # адрес почты целиком ловит паттерн EMAIL, и его домен не должен
        # маскироваться отдельным спаном WEB
        (
            r"(?<![\w@.-])(?:[\w-]+\.)+"
            r"(?:ru|su|com|net|org|io|info|biz|online|site|shop|tech|cloud"
            r"|pro|dev|app|рф|xn--p1ai)\b",
            0,
        ),
    ],
    "INN": [
        # ИНН физлица (12 цифр) или юрлица (10 цифр) — только рядом со словом ИНН/ОГРН/КПП
        r"\b(?:ИНН|ОГРН|КПП)\s*[:\s]*\s*(\d{10,12})\b",
        r"\b(\d{10,12})\b(?=\s*(?:ИНН|ОГРН|КПП|инн))",
    ],
    "MONEY": [
        # Сумма со словом валюты: "1 500 000,50 руб", "150000 руб"
        r"\b\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{2})?\s*"
        r"(?:руб\w*|₽|RUB|USD|EUR|долл\w*|евро)\b",
        # Сумма БЕЗ валюты — только сгруппированные разряды (пробел/NBSP):
        # в таблицах валюта стоит в заголовке колонки («Затраты, руб.»),
        # а ячейки — "83 600,00", "7 432 480,00". Группировка обязательна:
        # почтовые индексы (101000), годы и номера без пробелов не маскируются
        r"\b\d{1,3}(?:[ \u00A0\u2009]\d{3})+(?:[.,]\d{2})?\b",
        # Тысячи/миллионы с десятичной дробью: "1,5 млн рублей", "20 тыс. ₽"
        r"\b\d+(?:[.,]\d+)?\s*(?:тыс|млн|млрд)\b\.?(?:\s*(?:руб\w*|₽))?",
        # Валюты в обозначениях: "$1 500", "1 500 $", "1 000 евро"
        r"\$\s?\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d{2})?\b",
        r"\b\d{1,3}(?:[ \u00A0]\d{3})+(?:[.,]\d{2})?\s?(?:\$|€)\b",
        r"\b\d+(?:[.,]\d+)?\s?(?:долл\w*|евро|usd|eur)\b",
    ],
    "ORG": [
        # Организационно-правовая форма + название: "АО НПФ Благосостояние",
        # "ООО «Ромашка»" и т.п. Детерминированная страховка: такие
        # конструкции ловятся даже если LLM их пропустила.
        # Записи кортежем (паттерн, флаги): 0 = с учётом регистра, чтобы не
        # ловить бытовые фразы вроде "банк данных" или "фонд оплаты труда".
        (
            r"\b(?:ООО|ОАО|ЗАО|ПАО|НКО|НПФ|АО|ГК)\s+"
            r"(?:«[^»]{2,120}»|\"[^\"]{2,120}\"|"
            r"[А-ЯЁA-Z][\w\-]{1,80}(?:\s+[А-ЯЁA-Z][\w\-]{1,80}){0,3})",
            0,
        ),
        (
            r"\b(?:Фонд|Банк)\s+"
            r"(?:«[^»]{2,120}»|\"[^\"]{2,120}\"|"
            r"[А-ЯЁA-Z][\w\-]{1,80}(?:\s+[А-ЯЁA-Z][\w\-]{1,80}){0,3})",
            0,
        ),
    ],
}


class NERService:
    """Сервис для извлечения именованных сущностей"""

    # Верхняя граница кэша результатов NER (записей sha256(text) -> сущности)
    ENTITIES_CACHE_LIMIT = 128

    def __init__(self):
        # GlinerEngine сам читает настройки из NER_ENGINE (в т.ч. перекрытие
        # NER_CHUNK_OVERLAP_CHARS и бэкенд NER_BACKEND)
        self._engine = GlinerEngine()
        # Второй контур — Natasha/Slovnet: русский NER + yargy-ФИО.
        # Отключается NER_NATASHA=0; сбои Natasha не роняют анонимизацию
        # (GLiNER + regex остаются), а пишутся в WARNING.
        self._natasha: Optional[NatashaEngine] = (
            NatashaEngine() if NER_ENGINE.get("natasha") else None
        )
        # Кэш результатов NER: sha256(text) -> список сущностей.
        # Повторная анонимизация того же файла (новый чат/повтор запроса) не
        # должна снова гонять модель минуты: сущности детерминированы текстом,
        # а плейсхолдеры всё равно пересоздаются в маппингах новой сессии.
        # Кэш в памяти процесса — сбрасывается перезапуском прокси.
        self._entities_cache: dict[str, list] = {}

    @property
    def engine(self):
        """Доступ к GLiNER-движку (для ИИ-детектора чат-команд)."""
        return self._engine

    async def warmup(self):
        """Прогреть NER-движки (загрузить веса)."""
        await self._engine.warmup()
        if self._natasha:
            try:
                await self._natasha.warmup()
                logger.info("Natasha-контур загружен (%s)", self.natasha_info())
            except Exception as exc:  # Natasha не критична: GLiNER + regex остаются
                logger.warning(
                    "Natasha-контур не загрузился (анонимизация продолжит "
                    "работать на GLiNER + regex): %s", exc,
                )

    def is_available(self) -> bool:
        """Загружена ли основная локальная NER-модель."""
        return self._engine.is_available()

    def backend_info(self) -> str:
        """Бэкенды NER для /health и лога запуска."""
        info = self._engine.describe()
        if self._natasha:
            info += f"; natasha: {self.natasha_info()}"
        return info

    def natasha_info(self) -> str:
        """Статус Natasha-контура (для логов и /health)."""
        if not self._natasha:
            return "отключён"
        return self._natasha.describe()

    def _extract_regex_entities(self, text: str) -> list:
        """Извлечь сущности с помощью regex-паттернов (быстрый метод)"""
        from ..models.schemas import Entity

        entities = []

        for entity_type, patterns in REGEX_PATTERNS.items():
            for entry in patterns:
                # Запись может быть кортежем (паттерн, флаги): например,
                # оргформы ищутся с учётом регистра (флаги=0)
                if isinstance(entry, tuple):
                    pattern, flags = entry
                else:
                    pattern, flags = entry, re.IGNORECASE
                for match in re.finditer(pattern, text, flags):
                    start, end = match.start(), match.end()
                    value = match.group(0).strip()
                    if entity_type == "WEB":
                        # Хвостовая пунктуация предложения — не часть адреса
                        # («www.site.ru.» / «(www.site.ru)»)
                        while end > start and text[end - 1] in ".,;:!?)]}»\"'…":
                            end -= 1
                        value = text[start:end].strip()
                        if not value:
                            continue
                    entities.append(Entity(
                        text=value,
                        type=entity_type,
                        start=start,
                        end=end,
                        confidence=0.95
                    ))

        # Пересечения между regex-сущностями разных типов (домен внутри
        # URL/email) разрешаются в пользу более длинного/приоритетного спана
        return self._resolve_regex_overlaps(entities)

    # Приоритет типов при пересечении regex-спанов: меньшее число — выше
    # приоритет (email маскируется целиком, домен внутри него не дробится)
    _REGEX_TYPE_PRIORITY = {"EMAIL": 0, "WEB": 1}

    def _resolve_regex_overlaps(self, entities: list) -> list:
        """Убрать пересечения между regex-сущностями разных типов.

        Из перекрывающихся спанов остаётся более длинный; при равной длине —
        более приоритетный тип (_REGEX_TYPE_PRIORITY). Пример: домен внутри
        URL/email должен маскироваться одним спаном URL/email, а не отдельным
        [WEB_N] — иначе замена по оффсетам портит текст и round-trip.
        """
        if len(entities) < 2:
            return entities
        entities.sort(key=lambda e: (
            e.start, -(e.end - e.start),
            self._REGEX_TYPE_PRIORITY.get(e.type, 9),
        ))
        resolved: list = []
        for ent in entities:
            prev = resolved[-1] if resolved else None
            if prev is not None and ent.start < prev.end:
                cur_len = ent.end - ent.start
                prev_len = prev.end - prev.start
                cur_prio = self._REGEX_TYPE_PRIORITY.get(ent.type, 9)
                prev_prio = self._REGEX_TYPE_PRIORITY.get(prev.type, 9)
                if cur_len > prev_len or (cur_len == prev_len and cur_prio < prev_prio):
                    resolved[-1] = ent
                continue
            resolved.append(ent)
        return resolved

    def _validate_entity_offsets(self, text: str, ent_text: str, start: int, end: int) -> Optional[tuple[int, int]]:
        """
        Проверить/восстановить корректные оффсеты сущности в тексте.

        1. Если text[start:end] == ent_text — оффсеты верны.
        2. Иначе ищем точное вхождение ent_text в тексте.
        3. Если не найдено — возвращаем None (сущность отбрасывается).

        Returns:
            (start, end) или None
        """
        if not ent_text:
            return None

        # Проверка границ
        if 0 <= start < end <= len(text) and text[start:end] == ent_text:
            return start, end

        # Попытка найти точное вхождение
        idx = text.find(ent_text)
        if idx != -1:
            return idx, idx + len(ent_text)

        return None

    async def _extract_llm_entities(self, text: str) -> tuple[list, bool]:
        """
        Извлечь сущности локальной NER-моделью (GLiNER).

        Возвращает (сущности, failed). failed=True, если модель не
        загрузилась или упала — NER-детекция считается невыполненной.
        """
        categories = list(PII_CATEGORIES.keys())
        try:
            entities = await self._engine.predict(text, categories)
        except Exception as exc:
            logger.error(
                "NER-движок недоступен или упал: %s", exc, exc_info=True
            )
            self._last_engine_error = f"{type(exc).__name__}: {exc}"
            return [], True

        # Второй контур (Natasha/Slovnet): русский NER + детерминированные
        # ФИО. Сбой не делает NER «несостоявшимся» — GLiNER отработал.
        if self._natasha:
            try:
                extra = await self._natasha.predict(text, categories)
                if extra:
                    logger.info(
                        "Natasha-контур: %d доп. сущностей", len(extra)
                    )
                    entities = entities + extra
            except Exception as exc:
                logger.warning("Natasha-контур упал (пропускается): %s", exc)

        return entities, False

    # (Мёртвый _split_into_chunks эпохи LM Studio удалён: нарезкой чанков
    # занимается GlinerEngine._split_into_chunks.)

    # Визуально неразличимые пары кириллица/латиница («1С» и «1C»,
    # «Роснефть» и «Pоснефть»): модель может вернуть вариант с другим
    # алфавитом — нормализация позволяет найти оба написания.
    _HOMOGLYPH_TABLE = str.maketrans({
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
        "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h",
        "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    })

    @classmethod
    def _normalize_for_search(cls, s: str) -> str:
        """Нормализация для поиска: регистр не важен, ё приравнивается к е,
        визуально одинаковые буквы кириллицы/латиницы приравниваются"""
        return s.casefold().replace("ё", "е").translate(cls._HOMOGLYPH_TABLE)

    @staticmethod
    def _is_word_char(ch: str) -> bool:
        """Словесный символ (буква/цифра/подчёркивание) для проверки границ.

        Пустая строка (начало/конец текста) словесным символом не считается.
        """
        if not ch:
            return False
        return ch.isalnum() or ch == "_"

    @staticmethod
    def _is_acronym(value: str) -> bool:
        """Акроним: значение состоит только из заглавных букв/цифр/символов
        («ИС», «СЭД», «ГК», «АСУ ТП», «1С»). Для таких значений регистр
        значим: строчное «ис» — обычное слово, а не организация.
        """
        letters = [ch for ch in value if ch.isalpha()]
        return bool(letters) and all(ch.isupper() for ch in letters)


    def _locate_entity(self, text: str, ent_text: str) -> Optional[tuple[int, int, str]]:
        """
        Найти текст сущности в фрагменте; вернуть (start, end, текст как
        в оригинале) или None.

        Сначала ищется точное вхождение, затем — нормализованное
        (регистр, ё/е) на случай небольших искажений текста моделью.
        """
        if not ent_text:
            return None

        idx = text.find(ent_text)
        if idx != -1:
            return idx, idx + len(ent_text), ent_text

        n_text = self._normalize_for_search(text)
        n_ent = self._normalize_for_search(ent_text)
        idx = n_text.find(n_ent)
        if idx != -1 and idx + len(ent_text) <= len(text):
            return idx, idx + len(ent_text), text[idx:idx + len(ent_text)]

        return None
    def _merge_entities(self, regex_entities: list, llm_entities: list) -> list:
        """Объединить результаты от regex и LLM, убирая дубликаты"""
        all_entities = regex_entities.copy()

        for llm_ent in llm_entities:
            # Проверяем, нет ли пересечения с уже найденными
            overlapping = [
                existing
                for existing in all_entities
                if llm_ent.start <= existing.end and llm_ent.end >= existing.start
            ]
            if overlapping:
                # LLM-спан, накрывающий НЕСКОЛЬКО точных сущностей (например,
                # «Вдовин Д.В.\nИсполнитель\nНестеркин Ю.В.»), не заменяет их:
                # он «съедает» промежуточные слова документа. Точные оставляем.
                if len(overlapping) > 1:
                    continue
                existing = overlapping[0]
                # Иначе оставляем ту, что длиннее (более полную)
                if len(llm_ent.text) > len(existing.text):
                    all_entities.remove(existing)
                    all_entities.append(llm_ent)
                # если существующая длиннее или равна — дубликат, пропускаем

            else:
                all_entities.append(llm_ent)

        # Сортируем по позиции в тексте
        all_entities.sort(key=lambda e: e.start)

        return all_entities

    def _expand_all_occurrences(self, text: str, entities: list) -> list:
        """
        Расширить каждую уникальную сущность на ВСЕ её вхождения в тексте.

        Если значение признано PII хотя бы один раз, все его точные
        совпадения тоже подлежат замене.

        Вхождение признаётся только при соблюдении границ слова: значение
        не должно быть «приклеено» к соседним буквам (иначе короткие
        токены вроде «ИС» режут «рисками», «подпись», «система»).
        Для акронимов («ИС», «СЭД», «ГК», «1С») принимаются только
        вхождения, написанные заглавными буквами в оригинальном тексте.

        Приоритет у длинных значений: вхождения короткого значения,
        пересекающиеся с уже покрытым диапазоном, пропускаются.

        Returns:
            Новый отсортированный список сущностей (оффсеты в пределах text)
        """

        from ..models.schemas import Entity

        if not entities:
            return []

        unique: dict[str, Entity] = {}
        expanded: list[Entity] = []
        covered: list[tuple[int, int]] = []

        for ent in entities:
            value = ent.text
            # Короткие значения не расширяем на весь текст — слишком велик
            # риск ложных замен; оставляем только найденное вхождение
            if not value or len(value.strip()) < 2:
                expanded.append(ent)
                covered.append((ent.start, ent.end))
                continue
            if value not in unique:
                unique[value] = ent

        # Нормализованный поиск (регистр, ё/е, кириллица/латиница).
        # Нормализация сохраняет длину строки, поэтому оффсеты совпадают
        # с оригиналом и валидация границ выполняется по тем же индексам.
        norm_text = self._normalize_for_search(text)
        norm_ok = len(norm_text) == len(text)

        # Длинные значения обрабатываются раньше коротких
        for value in sorted(unique, key=len, reverse=True):
            ent = unique[value]
            acronym = self._is_acronym(value)
            norm_value = self._normalize_for_search(value)
            use_norm = norm_ok and len(norm_value) == len(value)
            search_in = norm_text if use_norm else text
            search_for = norm_value if use_norm else value

            head_is_word = self._is_word_char(value[:1])
            tail_is_word = self._is_word_char(value[-1:])

            pos = 0
            while True:
                idx = search_in.find(search_for, pos)
                if idx == -1:
                    break
                end = idx + len(value)

                # Границы слова: значение не должно быть частью соседнего
                # слова («ис» внутри «рисками», «ИС» внутри «ИСх№...»). Если
                # край значения — словесный символ, соседний символ с той же
                # стороны тоже должен быть несловесным (пробел, пунктуация,
                # начало/конец текста).
                prev_ch = search_in[idx - 1:idx]
                next_ch = search_in[end:end + 1]
                if ((head_is_word and self._is_word_char(prev_ch)) or
                        (tail_is_word and self._is_word_char(next_ch))):
                    pos = idx + 1  # кандидат отклонён — ищем дальше
                    continue

                # Акроним заменяем только написанный заглавными буквами:
                # автономное строчное «ис» или «Ис» — другое слово
                if acronym and any(ch.islower() for ch in text[idx:end]):
                    pos = idx + 1
                    continue

                pos = end
                # Пропускаем вхождения, пересекающиеся с уже покрытыми
                if any(s < end and e > idx for s, e in covered):
                    continue
                covered.append((idx, end))
                expanded.append(Entity(
                    text=value,
                    type=ent.type,
                    start=idx,
                    end=end,
                    confidence=ent.confidence,
                ))

        expanded.sort(key=lambda e: e.start)
        return expanded
    @staticmethod
    def _quoted_core(value: str) -> Optional[str]:
        """
        Извлечь «ядро» названия из значения в кавычках. Берётся последняя
        пара кавычек: для вложенных случаев вроде АО «НПФ «БЛАГОСОСТОЯНИЕ»
        ядром будет БЛАГОСОСТОЯНИЕ.
        """
        for oq, cq in (("«", "»"), ('"', '"')):
            oi = value.rfind(oq)
            ci = value.rfind(cq)
            if oi != -1 and ci > oi:
                core = value[oi + 1:ci].strip()
                if len(core) >= 2:
                    return core
        return None

    def _derive_quoted_cores(self, text: str, entities: list) -> list:
        """
        Вывод «ядра» названия организации из оргформы в кавычках
        (например, ООО «ИРИС» -> ИРИС): короткое ядро добавляется как
        сущность того же типа — тогда все разрозненные вхождения короткого
        названия будут заменены тем же плейсхолдером на этапе расширения.
        """
        from ..models.schemas import Entity

        known_texts = {e.text for e in entities}
        extra: list[Entity] = []
        for ent in entities:
            if ent.type != "ORG":
                continue
            core = self._quoted_core(ent.text)
            if not core or core in known_texts:
                continue
            idx = text.find(core)
            if idx == -1:
                continue
            known_texts.add(core)
            extra.append(Entity(
                text=core,
                type=ent.type,
                start=idx,
                end=idx + len(core),
                confidence=ent.confidence,
            ))
        if extra:
            logger.info("Добавлены ядра названий организаций: %s",
                        [e.text for e in extra])
        return entities + extra

    async def extract_entities(
        self,
        text: str,
        use_llm: bool = True
    ) -> tuple[list, float]:
        """
        Извлечь сущности из текста

        Args:
            text: Текст для анализа
            use_llm: Использовать ли NER-модель (иначе только regex)

        Returns:
            Кортеж (список сущностей, время обработки в мс)
        """
        entities, processing_time, _ = await self.extract_entities_detailed(
            text, use_llm
        )
        return entities, processing_time

    @property
    def last_error(self) -> str:
        """Текст последней ошибки NER-движка (для сообщений пользователю)."""
        return getattr(self, "_last_engine_error", "")

    async def extract_entities_detailed(
        self,
        text: str,
        use_llm: bool = True
    ) -> tuple[list, float, bool]:
        """
        Извлечь сущности из текста; дополнительно возвращает llm_failed.

        Returns:
            Кортеж (список сущностей, время обработки в мс, llm_failed).
            llm_failed=True — NER-модель не загрузилась/упала, значит
            результат неполон и его нельзя считать «анонимизировано».
        """
        start_time = time.time()

        # Кэш результатов NER по sha256(text): повторная анонимизация того же
        # текста (файла) не должна снова гонять модель минуту+.
        cache_key = None
        if use_llm:
            cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached = self._entities_cache.get(cache_key)
            if cached is not None:
                logger.info(
                    "NER: кэш-попадание по тексту (%d сущностей) — инференс пропущен",
                    len(cached),
                )
                return list(cached), 0.0, False

        # Уровень 1: Regex (всегда)
        regex_entities = self._extract_regex_entities(text)

        # Уровень 2: NER-модель (если включено)
        llm_entities: list = []
        llm_failed = False
        if use_llm:
            llm_entities, llm_failed = await self._extract_llm_entities(text)

        # Объединяем результаты
        merged_entities = self._merge_entities(regex_entities, llm_entities)

        # Уровень 2.5: вывод «ядра» названия организации из оргформы в
        # кавычках (ООО «ИРИС» -> ИРИС), чтобы короткие вхождения названия
        # тоже были заменены на этапе расширения
        merged_entities = self._derive_quoted_cores(text, merged_entities)

        # Уровень 3: расширение вхождений — если значение признано сущностью
        # хотя бы один раз, заменяются ВСЕ его точные вхождения в тексте
        all_entities = self._expand_all_occurrences(text, merged_entities)

        processing_time = (time.time() - start_time) * 1000

        # Кэшируем только успешные прогоны (llm_failed=True — результат неполон)
        if cache_key is not None and not llm_failed:
            self._store_entities_cache(cache_key, all_entities)

        return all_entities, processing_time, llm_failed

    def _store_entities_cache(self, key: str, entities: list) -> None:
        """Сохранить результат NER в кэш (FIFO-ограничение размера)."""
        if len(self._entities_cache) >= self.ENTITIES_CACHE_LIMIT:
            self._entities_cache.pop(next(iter(self._entities_cache)), None)
        self._entities_cache[key] = list(entities)

    async def close(self):
        """Освободить ресурсы"""
        await self._engine.close()
        if self._natasha:
            try:
                await self._natasha.close()
            except Exception:
                pass



