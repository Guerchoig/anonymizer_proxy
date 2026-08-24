"""
NER-сервис для извлечения именованных сущностей.

Использует локальный GLiNER-движок (in-process) как основной метод
и regex-паттерны как быстрый детерминированный слой. Оффсеты сущностей
GLiNER возвращает сам; для устойчивости сохранён поиск без учёта
регистра и различия ё/е.
"""
import logging
import re
import time
from typing import Optional

from ..config import NER_ENGINE, PII_CATEGORIES
from .gliner_engine import GlinerEngine

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
    "EMAIL": [
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    ],
    "INN": [
        # ИНН физлица (12 цифр) или юрлица (10 цифр) — только рядом со словом ИНН/ОГРН/КПП
        r"\b(?:ИНН|ОГРН|КПП)\s*[:\s]*\s*(\d{10,12})\b",
        r"\b(\d{10,12})\b(?=\s*(?:ИНН|ОГРН|КПП|инн))",
    ],
    "MONEY": [
        # Суммы денег
        r"\b\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{2})?\s*(?:руб|рубл|рублей|₽|RUB)\b",
        r"\b\d+\s*(?:тыс|млн|млрд)\.?\s*(?:руб|₽)?\b",
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

    def __init__(self):
        self._engine = GlinerEngine()

    async def warmup(self):
        """Прогреть локальную NER-модель (загрузить веса)."""
        await self._engine.warmup()

    def is_available(self) -> bool:
        """Загружена ли локальная NER-модель."""
        return self._engine.is_available()
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
                    entities.append(Entity(
                        text=match.group(0).strip(),
                        type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        confidence=0.95
                    ))

        return entities

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
            logger.error("NER-движок недоступен или упал: %s", exc)
            return [], True
        return entities, False

    def _split_into_chunks(self, text: str, max_chars: int) -> list[tuple[str, int]]:
        """
        Разбить текст на чанки не длиннее max_chars символов каждый.

        Возвращает список кортежей (кусок текста, смещение в исходном тексте).
        Границы чанков по возможности выравниваются по переводам строк
        (в пределах последних 20% чанка), соседние чанки перекрываются на
        chunk_overlap_chars, чтобы сущности на границе не терялись.
        """
        if len(text) <= max_chars:
            return [(text, 0)]

        overlap = min(NER_ENGINE["chunk_overlap_chars"], max_chars // 2)
        chunks: list[tuple[str, int]] = []
        total = len(text)
        start = 0
        while start < total:
            end = min(start + max_chars, total)
            if end < total:
                # Ищем ближайший перевод строки в последних 20% чанка,
                # чтобы не резать посреди строки/таблицы
                window_start = start + max_chars * 4 // 5
                newline_pos = text.rfind("\n", window_start, end)
                if newline_pos > start:
                    end = newline_pos + 1
            chunks.append((text[start:end], start))
            if end >= total:
                break
            start = end - overlap
        return chunks

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
            is_duplicate = False
            for existing in list(all_entities):
                # Если сущности пересекаются по позициям
                if (llm_ent.start <= existing.end and llm_ent.end >= existing.start):
                    # Оставляем ту, что длиннее (более полная)
                    if len(llm_ent.text) > len(existing.text):
                        all_entities.remove(existing)
                    else:
                        is_duplicate = True
                    break

            if not is_duplicate:
                all_entities.append(llm_ent)

        # Сортируем по позиции в тексте
        all_entities.sort(key=lambda e: e.start)

        return all_entities

    def _expand_all_occurrences(self, text: str, entities: list) -> list:
        """
        Расширить каждую уникальную сущность на ВСЕ её вхождения в тексте.

        Если значение признано PII хотя бы один раз, все его точные
        совпадения тоже подлежат замене.

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

        # Нормализованный поиск (регистр, ё/е, кириллица/латиница)
        norm_text = self._normalize_for_search(text)
        norm_ok = len(norm_text) == len(text)

        # Длинные значения обрабатываются раньше коротких
        for value in sorted(unique, key=len, reverse=True):
            ent = unique[value]
            norm_value = self._normalize_for_search(value)
            use_norm = norm_ok and len(norm_value) == len(value)
            search_in = norm_text if use_norm else text
            search_for = norm_value if use_norm else value
            pos = 0
            while True:
                idx = search_in.find(search_for, pos)
                if idx == -1:
                    break
                end = idx + len(value)
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

        return all_entities, processing_time, llm_failed

    async def close(self):
        """Освободить ресурсы"""
        await self._engine.close()



