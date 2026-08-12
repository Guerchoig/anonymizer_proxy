"""
Модуль замены сущностей на токены и обратной де-анонимизации
"""
import re
from typing import Optional

from ..models.schemas import Entity, MappingEntry


class TextReplacer:
    """Класс для анонимизации и де-анонимизации текста"""

    # Паттерн для поиска токенов в тексте
    TOKEN_PATTERN = re.compile(r'\[([A-Z_]+)_(\d+)\]')

    async def anonymize(
        self,
        text: str,
        entities: list[Entity],
        add_mapping_func  # async function(session_id, original_value, entity_type) -> token
    ) -> tuple[str, list[MappingEntry]]:
        """
        Заменить сущности в тексте на токены

        Args:
            text: Оригинальный текст
            entities: Список найденных сущностей (оффсеты ДОЛЖНЫ относиться к этому тексту)
            add_mapping_func: Функция для добавления маппинга

        Returns:
            Кортеж (анонимизированный текст, список созданных маппингов)
        """
        if not entities:
            return text, []

        # Отбрасываем сущности с оффсетами вне текста (защита от повреждения)
        valid_entities = [
            e for e in entities
            if 0 <= e.start < e.end <= len(text)
        ]
        if not valid_entities:
            return text, []

        # Сортируем сущности по позиции (от конца к началу для корректной замены)
        sorted_entities = sorted(valid_entities, key=lambda e: e.start, reverse=True)

        anonymized_text = text
        mappings = []

        # Кэш для уже созданных маппингов (чтобы не дублировать)
        value_to_token: dict[str, str] = {}

        for entity in sorted_entities:
            # Проверяем, есть ли уже токен для этого значения
            if entity.text in value_to_token:
                token = value_to_token[entity.text]
            else:
                # Создаём новый маппинг
                token = await add_mapping_func(entity.text, entity.type)
                value_to_token[entity.text] = token
                mappings.append(MappingEntry(
                    token=token,
                    original_value=entity.text,
                    entity_type=entity.type,
                    session_id=""  # Будет заполнено позже
                ))

            # Заменяем текст сущности на токен
            anonymized_text = (
                anonymized_text[:entity.start] +
                token +
                anonymized_text[entity.end:]
            )

        return anonymized_text, mappings

    async def deanonymize(
        self,
        text: str,
        mappings: dict[str, str]  # token -> original_value
    ) -> str:
        """
        Восстановить оригинальные значения из токенов

        Выполняется ОДНИМ проходом через единый regex:
        - нет проблемы с небезопасной строкой замены re.sub;
        - нет каскадных замен (если значение само содержит токен).

        Args:
            text: Анонимизированный текст (или ответ от модели)
            mappings: Словарь маппингов (token -> original_value)

        Returns:
            Де-анонимизированный текст
        """
        if not mappings or not text:
            return text

        # Экранируем все токены и объединяем в один паттерн.
        # Сортировка по длине (убывание) — чтобы более длинные токены
        # не были частично заменены более короткими.
        sorted_tokens = sorted(mappings.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(t) for t in sorted_tokens))

        # lambda-замена безопасна: значение подставляется как есть
        return pattern.sub(lambda m: mappings[m.group(0)], text)

    def find_tokens(self, text: str) -> list[tuple[str, str, int]]:
        """
        Найти все токены в тексте

        Returns:
            Список кортежей (token, entity_type, position)
        """
        matches = []
        for match in self.TOKEN_PATTERN.finditer(text):
            full_token = match.group(0)
            entity_type = match.group(1)
            matches.append((full_token, entity_type, match.start()))
        return matches

    def has_tokens(self, text: str) -> bool:
        """Проверить, есть ли токены в тексте"""
        return bool(self.TOKEN_PATTERN.search(text))


class StreamDeAnonymizer:
    """
    Буферизованная де-анонимизация для стриминга.

    Проблема: токен вида [PERSON_1] может прийти разорванным на границе
    чанков ("[PER" + "SON_1]"). Простая замена по каждому чанку пропустит
    такой токен, и плейсхолдер утечёт к клиенту.

    Решение: накапливаем текст; отдаём клиенту только "безопасную" часть —
    до последнего потенциального начала токена ('['). Хвост с незакрытым '['
    удерживаем до следующего чанка. В конце потока сбрасываем остаток.
    """

    def __init__(self, replacer: TextReplacer, mappings: dict[str, str]):
        self.replacer = replacer
        self.mappings = mappings or {}
        self._buffer = ""

    async def feed(self, chunk_text: str) -> str:
        """
        Добавить новый фрагмент текста и вернуть текст, который можно
        безопасно отправить клиенту (уже де-анонимизированный).
        """
        if not chunk_text:
            return ""

        self._buffer += chunk_text

        # Ищем последний потенциальный начало токена
        last_open = self._buffer.rfind("[")
        if last_open == -1:
            # Токенов нет — отдаём весь буфер
            safe, self._buffer = self._buffer, ""
        else:
            # Проверяем, закрыт ли последний '['
            close_after = self._buffer.find("]", last_open)
            if close_after != -1:
                # Закрыт — потенциальный токен целиком в буфере, отдаём всё
                safe, self._buffer = self._buffer, ""
            else:
                # Незакрытый '[' в конце — удерживаем хвост
                safe, self._buffer = self._buffer[:last_open], self._buffer[last_open:]

        if not safe:
            return ""

        return await self.replacer.deanonymize(safe, self.mappings)

    async def flush(self) -> str:
        """Сбросить остаток буфера в конце потока"""
        if not self._buffer:
            return ""
        rest, self._buffer = self._buffer, ""
        return await self.replacer.deanonymize(rest, self.mappings)


class MessageAnonymizer:
    """Анонимизатор для сообщений чата (OpenAI формат)"""

    def __init__(self, text_replacer: TextReplacer):
        self.replacer = text_replacer

    async def anonymize_message_content(
        self,
        content: str | list[dict],
        entities: list[Entity],
        add_mapping_func
    ) -> tuple[str | list[dict], list[MappingEntry]]:
        """
        Анонимизировать содержимое сообщения

        ВАЖНО: entities должны содержать оффсеты относительно именно этого
        контента (см. handlers._entities_for_segment).

        Поддерживает:
        - Простой текст (str)
        - Мультимодальный контент (list[dict] с type: text/image_url)
        """
        if isinstance(content, str):
            return await self.replacer.anonymize(content, entities, add_mapping_func)

        elif isinstance(content, list):
            # Мультимодальный контент: оффсеты считаются по конкатенации
            # текстовых частей через "\n" (см. handlers)
            text_parts: list[tuple[int, str]] = []  # (индекс части, текст)
            for idx, part in enumerate(content):
                if part.get("type") == "text" and "text" in part:
                    text_parts.append((idx, part["text"]))

            anonymized_parts = []
            all_mappings = []

            if text_parts:
                # Строим сегменты для пересчёта оффсетов
                segments = [t for _, t in text_parts]
                segment_entities = split_entities_by_segments(entities, segments)

                for (idx, text), seg_entities in zip(text_parts, segment_entities):
                    anon_text, mappings = await self.replacer.anonymize(
                        text, seg_entities, add_mapping_func
                    )
                    anonymized_parts.append({
                        "type": "text",
                        "text": anon_text
                    })
                    all_mappings.extend(mappings)
                # Заполняем неместо... ниже — простая схема: проходим заново
                # (собираем результат в порядке исходных частей)
                result_parts = []
                anon_iter = iter(anonymized_parts)
                for part in content:
                    if part.get("type") == "text" and "text" in part:
                        result_parts.append(next(anon_iter))
                    else:
                        result_parts.append(part)
                return result_parts, all_mappings

            return content, []

        return content, []

    async def deanonymize_message_content(
        self,
        content: str | list[dict],
        mappings: dict[str, str]
    ) -> str | list[dict]:
        """Де-анонимизировать содержимое сообщения"""
        if isinstance(content, str):
            return await self.replacer.deanonymize(content, mappings)

        elif isinstance(content, list):
            deanonymized_parts = []
            for part in content:
                if part.get("type") == "text" and "text" in part:
                    dean_text = await self.replacer.deanonymize(part["text"], mappings)
                    deanonymized_parts.append({
                        "type": "text",
                        "text": dean_text
                    })
                else:
                    deanonymized_parts.append(part)
            return deanonymized_parts

        return content


def split_entities_by_segments(
    entities: list[Entity],
    segments: list[str],
    separator: str = "\n",
) -> list[list[Entity]]:
    """
    Разнести сущности, найденные по конкатенированному тексту
    (sep.join(segments)), по отдельным сегментам с пересчётом оффсетов.

    Это ключевая функция для корректной анонимизации нескольких сообщений:
    NER выполняется один раз по склеенному тексту, а замена происходит
    в каждом сообщении отдельно — оффсеты должны быть локальными.

    Args:
        entities: Сущности с оффсетами относительно склеенного текста
        segments: Исходные текстовые сегменты (в порядке склейки)
        separator: Разделитель, использованный при склейке

    Returns:
        Список списков сущностей для каждого сегмента (с локальными оффсетами)
    """
    result: list[list[Entity]] = [[] for _ in segments]

    # Границы сегментов в склеенном тексте
    boundaries: list[tuple[int, int]] = []  # (start, end) каждого сегмента
    pos = 0
    for i, seg in enumerate(segments):
        start = pos
        end = pos + len(seg)
        boundaries.append((start, end))
        pos = end + len(separator)

    for ent in entities:
        # Определяем, к какому сегменту относится сущность
        for i, (seg_start, seg_end) in enumerate(boundaries):
            if ent.start >= seg_start and ent.end <= seg_end:
                local_start = ent.start - seg_start
                local_end = ent.end - seg_start
                result[i].append(Entity(
                    text=ent.text,
                    type=ent.type,
                    start=local_start,
                    end=local_end,
                    confidence=ent.confidence,
                ))
                break
            # Сущность пересекает границу сегментов — пытаемся привязать
            # по большему перекрытию
            if ent.start < seg_end and ent.end > seg_start:
                overlap = min(ent.end, seg_end) - max(ent.start, seg_start)
                ent_len = ent.end - ent.start
                if ent_len > 0 and overlap / ent_len >= 0.5:
                    # Обрезаем до границ сегмента и пересчитываем текст
                    local_start = max(ent.start, seg_start) - seg_start
                    local_end = min(ent.end, seg_end) - seg_start
                    local_text = segments[i][local_start:local_end]
                    if local_text.strip():
                        result[i].append(Entity(
                            text=local_text,
                            type=ent.type,
                            start=local_start,
                            end=local_end,
                            confidence=ent.confidence * 0.8,
                        ))
                    break

    return result