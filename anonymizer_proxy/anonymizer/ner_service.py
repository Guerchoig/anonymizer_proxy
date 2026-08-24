"""
NER-сервис для извлечения именованных сущностей
Использует LM Studio как основной метод (поддержан thinking-режим:
если итоговый ответ в content пуст, JSON дополнительно ищется в
reasoning_content) и regex-паттерны как быстрый fallback.

Модель возвращает только текст и тип сущностей; оффсеты в тексте
вычисляются самим прокси (точное совпадение + поиск без учёта
регистра и различия ё/е).
"""
import asyncio
import json
import logging
import re
import time
from typing import Optional
import httpx

from ..config import LM_STUDIO, PII_CATEGORIES

logger = logging.getLogger("anonymizer_proxy.ner")


class NERUnavailableError(ValueError):
    """LM Studio недоступна или не вернула результат — LLM-детекция не выполнена.

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

# Промпт для LM Studio NER
NER_SYSTEM_PROMPT = """Ты — система распознавания именованных сущностей (NER) для русского языка.
Твоя задача — извлечь из текста все конфиденциальные данные.

Категории для извлечения:
- PERSON: Имена, фамилии, отчества людей
- POSITION: Должности (генеральный директор, главный бухгалтер, менеджер)
- DEPARTMENT: Подразделения (отдел кадров, бухгалтерия, цех №5, департамент)
- ORG: Названия компаний, организаций, юрлиц (компания, фирма, ООО, АО, ПАО, НПФ, ОАО). 
- LOC: Географические названия (города, улицы, адреса, регионы)
- PRODUCT: Названия продуктов, систем, проектов (1С:Предприятие, SAP)
- PASSPORT: Паспортные данные (серия, номер, кем и когда выдан)
- PHONE: Телефоны
- EMAIL: Email адреса
- INN: ИНН, ОГРН, КПП и другие идентификаторы
- MONEY: Суммы денег

ВАЖНО:
1. Извлекай сущности из ВСЕГО текста, включая пути и имена файлов, названия
   проектов и документов, заголовки таблиц, должности, подписи.
2. Сущности могут встречаться внутри других названий: например, имя файла
   "КП для ООО 'Ромашка'.docx" или название проекта "Внедрение ЕРП в ООО
   'Ромашка'" содержат сущность ORG Ромашка.
3. Копируй текст сущности ТОЧНО как в оригинале: не меняй регистр, падеж,
   кавычки, пробелы и язык букв («1С» с кириллической С и «1C» с латинской
   C — РАЗНЫЕ строки; копируй так, как написано в тексте).
4. Извлекай ТОЛЬКО реальные сущности, не выдумывай.
5. Извлекай ВСЕ организации, включая широко известные (ЛУКОЙЛ, Газпром,
   Сбербанк и т.п.) и государственные органы (Росархив, Минцифры, ФНС и т.п.):
   известность не делает название неконфиденциальным.
6. Имена и фамилии с инициалами (М.В. Келдыш, Иванов И.И.) — это PERSON,
   в том числе внутри названий организаций («…имени М.В. Келдыша»).
7. Если одна организация упомянута под разными названиями, аббревиатурами
   или в разных падежах — укажи каждый уникальный вариант, встречающийся
   в тексте.
8. Даты, номера версий и этапов — НЕ MONEY; MONEY — только денежные суммы.
9. Каждое уникальное значение достаточно указать ОДИН раз. Позицию (оффсеты)
   указывать НЕ нужно — точные позиции в тексте программа определит сама.
11. Возвращай результат СТРОГО в JSON формате.

Формат ответа:
{
  "entities": [
    {"text": "Иванов Иван Иванович", "type": "PERSON"},
    {"text": "ООО \"Ромашка\"", "type": "ORG"}
  ]
}

Если сущностей не найдено, верни: {"entities": []}"""


class NERService:
    """Сервис для извлечения именованных сущностей"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        # Кэш проверки доступности LM Studio
        self._lm_available: bool = False
        self._lm_checked_at: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=LM_STUDIO["timeout"])
        return self._client

    async def check_lm_studio(self, force: bool = False) -> bool:
        """
        Проверить доступность LM Studio.
        Результат кэшируется на LM_STUDIO["availability_cache_seconds"] секунд,
        чтобы не делать HTTP-вызов на каждый запрос.
        """
        cache_ttl = LM_STUDIO.get("availability_cache_seconds", 10.0)
        now = time.monotonic()
        if not force and (now - self._lm_checked_at) < cache_ttl:
            return self._lm_available

        try:
            client = await self._get_client()
            response = await client.get(f"{LM_STUDIO['base_url']}/models")
            self._lm_available = response.status_code == 200
            if not self._lm_available:
                logger.warning("LM Studio вернул статус %s", response.status_code)
        except Exception as e:
            logger.warning("LM Studio недоступен: %s", e)
            self._lm_available = False
        finally:
            self._lm_checked_at = now

        return self._lm_available

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

        LLM часто ошибаются в позициях, поэтому:
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
        Извлечь сущности с помощью LM Studio (точный метод).

        Текст длиннее LM_STUDIO["max_input_chars"] разбивается на чанки
        с перекрытием (см. _split_into_chunks); результаты всех чанков
        переносятся в координаты исходного текста и дедуплицируются
        (зона перекрытия может дать одинаковые сущности).

        Возвращает (сущности, llm_failed). llm_failed=True, если LM Studio
        недоступна или хотя бы один чанк не дал результата (обрыв соединения,
        таймаут, отсутствие JSON) — LLM-детекция считается невыполненной.
        """
        from ..models.schemas import Entity

        if not await self.check_lm_studio():
            logger.info("LM Studio недоступен, пропускаем LLM-извлечение")
            return [], True

        max_input = max(1000, LM_STUDIO["max_input_chars"])
        chunks = self._split_into_chunks(text, max_input)
        if len(chunks) > 1:
            logger.info(
                "Текст %d символов больше лимита %d — разбит на %d чанков",
                len(text), max_input, len(chunks),
            )

        # Обработка чанков с ограничением числа одновременных запросов
        # (ner_parallel). По умолчанию 1 (последовательно): конкурентные запросы
        # к LM Studio могут вешать модель. asyncio.gather сохраняет порядок
        # результатов — дедупликация ниже остаётся детерминированной.
        ner_parallel = max(1, int(LM_STUDIO.get("ner_parallel", 1)))
        sem = asyncio.Semaphore(ner_parallel)

        async def process(chunk_text: str):
            async with sem:
                return await self._llm_ner_once(chunk_text)

        chunk_results = await asyncio.gather(
            *(process(chunk_text) for chunk_text, _offset in chunks)
        )

        all_entities: list = []
        llm_failed = False
        seen: set = set()
        for chunk_index, (
            (_chunk_text, chunk_offset), (chunk_entities, ok)
        ) in enumerate(zip(chunks, chunk_results)):
            if not ok:
                llm_failed = True
            for ent in chunk_entities:
                # Перенос оффсетов из координат чанка в координаты текста
                shifted = Entity(
                    text=ent.text,
                    type=ent.type,
                    start=ent.start + chunk_offset,
                    end=ent.end + chunk_offset,
                    confidence=ent.confidence,
                )
                key = (shifted.start, shifted.end, shifted.text, shifted.type)
                if key in seen:
                    continue  # дубль из зоны перекрытия чанков
                seen.add(key)
                all_entities.append(shifted)
            if len(chunks) > 1:
                logger.info(
                    "Чанк %d/%d: %d сущностей",
                    chunk_index + 1, len(chunks), len(chunk_entities),
                )

        return all_entities, llm_failed

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

        overlap = min(LM_STUDIO["chunk_overlap_chars"], max_chars // 2)
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

    @staticmethod
    def _balanced_json_at(raw: str, start: int) -> Optional[str]:
        """Вырезать JSON-объект с позиции start по балансу скобок
        (с учётом строк в кавычках). None, если объект не закрыт."""
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
        return None

    def _extract_entities_json(self, raw: str) -> Optional[list]:
        """
        Извлечь список entities из JSON в ответе модели.

        Поддерживает thinking-режим: JSON может быть окружён рассуждениями,
        а в тексте может быть несколько JSON-объектов (промежуточные и
        итоговый) — берётся последний корректный.
        """
        if not raw or '"entities"' not in raw:
            return None

        # Чистый JSON без примесей
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("entities"), list):
                return data["entities"]
        except json.JSONDecodeError:
            pass

        # Перебор кандидатов с конца: итоговый JSON — последний
        for m in reversed(list(re.finditer(r'\{\s*"entities"\s*:', raw))):
            candidate = self._balanced_json_at(raw, m.start())
            if candidate is None:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("entities"), list):
                return data["entities"]
        return None

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

        Оффсеты сущностей прокси вычисляет сам — модель возвращает только
        text и type. Сначала ищется точное вхождение, затем — нормализованное
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

    async def _llm_ner_once(self, chunk_text: str) -> tuple[list, bool]:
        """
        Один NER-вызов к LM Studio по фрагменту текста.

        Поддерживает thinking-режим: если итоговый ответ в content пуст,
        JSON ищется в reasoning_content. Модель возвращает только text и
        type сущностей; оффсеты вычисляются прокси (_locate_entity).

        Возвращает (сущности, ok) — сущности с оффсетами относительно
        chunk_text и признак успеха LLM-вызова (False при обрыве соединения,
        таймауте или отсутствии JSON).
        """
        from ..models.schemas import Entity

        logger.info("Отправляем запрос в LM Studio (%d символов)...", len(chunk_text))

        try:
            payload = {
                "model": LM_STUDIO["model"],
                "messages": [
                    {"role": "system", "content": NER_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Извлеки все сущности из следующего текста:\n\n{chunk_text}"}
                ],
                "temperature": LM_STUDIO["temperature"],
                "max_tokens": LM_STUDIO["max_tokens"],
            }

            result = await self._request_ner(payload)
            if result is None:
                # Таймаут или ошибка соединения: соединение с LM Studio уже
                # разорвано (см. _http_post_json), модель остановлена.
                return [], False

            logger.info("LM Studio ответил успешно")
            choice = result["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            # Thinking-режим: рассуждения модели приходят в отдельном поле
            reasoning = message.get("reasoning_content") or ""

            entities_data = self._extract_entities_json(content)
            source = "content"
            if entities_data is None and reasoning:
                entities_data = self._extract_entities_json(reasoning)
                source = "reasoning_content"

            if entities_data is None:
                logger.warning(
                    "LM Studio не вернул JSON с сущностями (finish_reason=%s, "
                    "content пуст: %s, reasoning: %d символов). Возможная "
                    "причина: thinking не успел завершиться в лимите токенов — "
                    "увеличьте Reasoning budget в LM Studio или NER_MAX_TOKENS",
                    choice.get("finish_reason"), not content, len(reasoning),
                )
                return [], False

            logger.info("Разобран ответ модели из поля %s: %d сущностей",
                        source, len(entities_data))

            entities = []
            skipped = 0
            for ent in entities_data:
                # Контракт: модель возвращает только text и type — оффсеты
                # прокси вычисляет сам (см. _locate_entity)
                if not isinstance(ent, dict):
                    skipped += 1
                    continue
                ent_text = str(ent.get("text") or "").strip()
                ent_type = str(ent.get("type") or "").upper().strip()
                if not ent_text or not ent_type:
                    skipped += 1
                    continue

                located = self._locate_entity(chunk_text, ent_text)
                if located is None:
                    skipped += 1
                    logger.debug(
                        "Сущность отброшена (текст не найден в фрагменте): %r",
                        ent_text
                    )
                    continue

                start, end, matched = located
                entities.append(Entity(
                    text=matched,
                    type=ent_type,
                    start=start,
                    end=end,
                    confidence=float(ent.get("confidence", 0.9))
                ))

            if skipped:
                logger.warning("Отброшено сущностей (текст не найден): %d", skipped)

            return entities, True

        except Exception as e:
            logger.error("Ошибка при вызове LM Studio: %s", e)
            return [], False

    async def _request_ner(self, payload: dict) -> Optional[dict]:
        """Отправить NER-запрос в LM Studio и вернуть распарсенный JSON-ответ.

        Вынесено в отдельный метод для подмены в тестах. В продакшене запрос
        выполняется через ``_http_post_json`` — сырой asyncio-сокет с жёстким
        таймаутом, который при таймауте гарантированно разрывает соединение.
        """
        return await self._http_post_json(payload)

    async def _http_post_json(self, payload: dict) -> Optional[dict]:
        """POST JSON в LM Studio через сырой asyncio-сокет.

        По истечении ``NER_TIMEOUT_SECONDS`` сокет закрывается, поэтому
        LM Studio получает разрыв соединения и останавливает генерацию.
        Возвращает распарсенный JSON-ответ (dict) при HTTP 200, иначе None.
        """
        from urllib.parse import urlsplit

        base_url = LM_STUDIO["base_url"]
        parts = urlsplit(base_url)
        scheme = (parts.scheme or "http").lower()
        if scheme != "http":
            logger.warning("LM Studio поддерживает только http:// (получен %r)", scheme)
            return None
        host = parts.hostname
        if not host:
            logger.warning("Некорректный LM_STUDIO_URL: %r", base_url)
            return None
        port = parts.port or 80
        base_path = parts.path.rstrip("/") or ""
        path = f"{base_path}/chat/completions"
        timeout = float(LM_STUDIO["timeout"])
        connect_timeout = min(timeout, 15.0)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_bytes = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body

        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
            writer.write(request_bytes)
            await asyncio.wait_for(writer.drain(), timeout=connect_timeout)

            status_code, body_bytes = await asyncio.wait_for(
                self._read_http_response(reader), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(
                "ТАЙМАУТ NER через %.0fс — разрываю соединение с LM Studio, "
                "чтобы модель остановила генерацию", timeout,
            )
            await self._close_writer(writer)
            return None
        except Exception as e:
            logger.error("Ошибка при обращении к LM Studio: %s", e)
            await self._close_writer(writer)
            return None

        await self._close_writer(writer)

        if status_code != 200:
            logger.warning(
                "LM Studio вернул ошибку %s: %.200s",
                status_code, body_bytes.decode("utf-8", "replace"),
            )
            return None
        try:
            result = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("LM Studio вернул некорректный JSON")
            return None
        if not isinstance(result, dict):
            logger.warning("LM Studio вернул неожиданный JSON-ответ")
            return None
        return result

    async def _read_http_response(self, reader) -> tuple[int, bytes]:
        """Прочитать HTTP-ответ: (статус, тело).

        Поддерживает Content-Length, chunked и ответ до закрытия соединения.
        """
        head = await self._read_until(reader, b"\r\n\r\n")
        status_code = 0
        headers: dict = {}
        try:
            lines = head.decode("latin-1", "replace").split("\r\n")
        except Exception:
            lines = []
        if lines:
            parts = lines[0].split(" ")
            if len(parts) >= 2:
                try:
                    status_code = int(parts[1])
                except ValueError:
                    status_code = 0
        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        transfer = headers.get("transfer-encoding", "").lower()
        if "chunked" in transfer:
            body = await self._read_chunked_body(reader)
        else:
            content_length = None
            try:
                content_length = int(headers.get("content-length", ""))
            except (TypeError, ValueError):
                content_length = None
            if content_length is not None:
                body = await reader.readexactly(content_length)
            else:
                body = await reader.read(-1)

        return status_code, body

    async def _read_chunked_body(self, reader) -> bytes:
        """Прочитать тело ответа в chunked-кодировании."""
        chunks = []
        while True:
            size_line = (await self._read_until(reader, b"\r\n")).strip()
            try:
                size = int(size_line.split(b";", 1)[0], 16)
            except (ValueError, IndexError):
                break
            if size == 0:
                # Хвост: trailer-заголовки + закрытие (Connection: close)
                await reader.read(-1)
                break
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)  # CRLF после чанка
        return b"".join(chunks)

    @staticmethod
    async def _read_until(reader, delimiter: bytes) -> bytes:
        """Читать из потока до появления delimiter (включительно)."""
        buffer = b""
        while delimiter not in buffer:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buffer += chunk
        return buffer

    @staticmethod
    async def _close_writer(writer) -> None:
        """Закрыть сокет; при сбое штатного закрытия — принудительный abort."""
        if writer is None:
            return
        try:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                transport = getattr(writer, "transport", None)
                if transport is not None:
                    transport.abort()
        except Exception:
            pass

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

        LLM (и regex) часто отмечают только часть вхождений одного и того же
        значения: например, название компании встречается в документе десятки
        раз, а в списке сущностей — один-два. Если значение признано PII
        хотя бы один раз, все его точные совпадения тоже подлежат замене.

        Приоритет у длинных значений: вхождения короткого значения,
        пересекающиеся с уже покрытым диапазоном (например, "Благосостояние"
        внутри "АО НПФ Благосостояние"), пропускаются.

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

        # Нормализованный поиск (регистр, ё/е, кириллица/латиница): индексы
        # в нормализованном тексте совпадают с оригиналом, т.к. нормализация
        # не меняет длину. Если длина всё же изменилась (редкие unicode) —
        # используется точный поиск.
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
        Дополнить список сущностей «ядрами» названий организаций.

        Если сущность ORG содержит название в кавычках (например,
        ООО «ИРИС» -> ИРИС), короткое ядро добавляется как сущность того же
        типа — тогда все разрозненные вхождения короткого названия будут
        заменены тем же плейсхолдером на этапе расширения вхождений
        (иначе короткие вхождения утекают в облако).
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
            use_llm: Использовать ли LLM (иначе только regex)

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
            llm_failed=True — LLM-детекция не выполнена (LM Studio недоступна,
            обрыв соединения или нет JSON), значит результат неполон и его
            нельзя считать «анонимизировано».
        """
        start_time = time.time()

        # Уровень 1: Regex (всегда)
        regex_entities = self._extract_regex_entities(text)

        # Уровень 2: LLM (если включено и доступно)
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
        """Закрыть HTTP клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()