"""
Обработчики запросов для прокси-сервера
Содержит основную логику анонимизации/де-анонимизации
"""
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import unquote, urlparse

from ..anonymizer.ner_service import NERService
from ..anonymizer.file_parser import FileParser, FileAssembler
from ..anonymizer.mapping_store import MappingStore
from ..anonymizer.replacer import (
    TextReplacer,
    MessageAnonymizer,
    StreamDeAnonymizer,
    split_entities_by_segments,
)
from ..config import Mode, CURRENT_MODE, STORAGE, RESULT_BEGIN, RESULT_END, BASE_DIR
from ..models.schemas import (
    Entity,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChoice,
    ChatMessage,
    UsageInfo,
    AnonymizeRequest,
    AnonymizeResponse,
    SendAnonymizedRequest,
)
from .openrouter_client import OpenRouterClient

logger = logging.getLogger("anonymizer_proxy.handlers")

# Блоки <file_content path="...">...</file_content>, которые клиент (Cline)
# вставляет в текст сообщения при прикладывании файлов
FILE_CONTENT_BLOCK_RE = re.compile(
    r'<file_content path="(?P<path>[^"]+)">(?P<body>.*?)</file_content>',
    re.DOTALL,
)
# Префикс заглушки-ошибки: клиент не смог прочитать файл (обычно бинарный
# документ вида DOCX/XLSX) и прислал вместо содержимого текст ошибки
FILE_CONTENT_ERROR_PREFIX = "Error fetching content"
# Корень рабочей области для относительных путей из <file_content path="...">
# (BASE_DIR — корень проекта, он же workspace; сервер запускается из него)
WORKSPACE_ROOT = BASE_DIR


class PreparedRequest:
    """Подготовленные данные для стриминга (NER + анонимизация уже выполнены)"""

    def __init__(
        self,
        session_id: str,
        anonymized_messages: list[dict],
        mappings_dict: dict,
        entities: list,
        original_content: str,
        anonymized_content: str,
        canonical_result: str,
    ):
        self.session_id = session_id
        self.anonymized_messages = anonymized_messages
        self.mappings_dict = mappings_dict
        self.entities = entities
        self.original_content = original_content
        self.anonymized_content = anonymized_content
        # Канонический результат: markdown-рендер анонимизированных сообщений.
        # Это проверяемая проекция контекста, отправляемого в облако:
        # содержимое файла data/anonymized_files/... и текст между маркерами
        # RESULT_BEGIN/RESULT_END идентичны canonical_result.
        self.canonical_result = canonical_result


def _collect_message_segments(
    messages: list[ChatMessage],
) -> tuple[list[str], list[int], list[list[tuple[int, str, int]]]]:
    """
    Собрать текстовые сегменты для NER — по одному на сообщение.

    Сегмент сообщения = текст content + аргументы tool_calls, соединённые
    через '\\n'. Аргументы tool_calls тоже могут содержать PII (это тексты
    из предыдущих ответов модели), поэтому они участвуют в анонимизации.

    Returns:
        (segments, content_lens, args_meta_all):
        - segments[i]: полный текст сообщения i (для NER)
        - content_lens[i]: длина текстовой части content внутри сегмента
        - args_meta_all[i]: список кортежей (tc_index, args_str, offset) —
          где offset = позиция args_str внутри сегмента
    """
    segments: list[str] = []
    content_lens: list[int] = []
    args_meta_all: list[list[tuple[int, str, int]]] = []

    for msg in messages:
        # Текстовая часть content (для мультимодальных — только text-части)
        if isinstance(msg.content, str):
            content_text = msg.content
        elif isinstance(msg.content, list):
            parts = [
                part.get("text", "")
                for part in msg.content
                if part.get("type") == "text"
            ]
            content_text = "\n".join(parts)
        else:
            content_text = ""

        seg_parts = [content_text]
        args_meta: list[tuple[int, str, int]] = []
        pos = len(content_text)

        for tc_index, tc in enumerate(msg.tool_calls or []):
            fn = tc.get("function") if isinstance(tc, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str) and args:
                args_meta.append((tc_index, args, pos + 1))  # +1: разделитель '\n'
                seg_parts.append(args)
                pos += 1 + len(args)

        segments.append("\n".join(seg_parts))
        content_lens.append(len(content_text))
        args_meta_all.append(args_meta)

    return segments, content_lens, args_meta_all


def _render_messages_markdown(anonymized_messages: list[dict]) -> str:
    """
    Каноническое представление анонимизированного запроса (Markdown).

    Это ЕДИНСТВЕННОЕ представление результата анонимизации: оно пишется
    в файл, возвращается между маркерами RESULT_BEGIN/RESULT_END и является
    проверяемой проекцией контекста, реально отправляемого в облако.
    """
    parts: list[str] = []
    for msg in anonymized_messages:
        role = msg.get("role", "?")
        parts.append(f"## {role}")
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                parts.append(f"> tool_call: {fn.get('name')} args={fn.get('arguments')}")
    return "\n\n".join(p for p in parts if p)


class RequestHandler:
    """Основной обработчик запросов с анонимизацией"""

    def __init__(
        self,
        ner_service: NERService,
        mapping_store: MappingStore,
        openrouter_client: OpenRouterClient,
    ):
        self.ner = ner_service
        self.store = mapping_store
        self.openrouter = openrouter_client
        self.file_parser = FileParser()
        self.file_assembler = FileAssembler()
        self.text_replacer = TextReplacer()
        self.message_anonymizer = MessageAnonymizer(self.text_replacer)

    async def _resolve_failed_file_contents(
        self,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        """
        Подменить содержимое блоков <file_content>, которые клиент не смог
        прочитать (тело начинается с "Error fetching content"), текстом,
        извлечённым прокси напрямую из локального файла через FileParser.

        Срабатывает, например, когда Cline прикладывает бинарный документ
        (DOCX/XLSX), но не может включить его в контекст и присылает вместо
        содержимого заглушку с ошибкой. Без этого документ терялся бы:
        его текст не попадал ни в NER/анонимизацию, ни в облако.
        """
        resolved: list[ChatMessage] = []
        for msg in messages:
            if (
                isinstance(msg.content, str)
                and FILE_CONTENT_ERROR_PREFIX in msg.content
            ):
                msg = msg.model_copy(update={
                    "content": await self._substitute_failed_blocks(msg.content)
                })
            elif isinstance(msg.content, list):
                new_parts = []
                changed = False
                for part in msg.content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and FILE_CONTENT_ERROR_PREFIX in part.get("text", "")
                    ):
                        new_parts.append({
                            **part,
                            "text": await self._substitute_failed_blocks(
                                part["text"]
                            ),
                        })
                        changed = True
                    else:
                        new_parts.append(part)
                if changed:
                    msg = msg.model_copy(update={"content": new_parts})
            resolved.append(msg)
        return resolved

    async def _substitute_failed_blocks(self, text: str) -> str:
        """
        Заменить тела неудачно прочитанных клиентом <file_content>-блоков
        на текст, извлечённый прокси из локального файла. Остальные блоки
        (успешно прочитанные клиентом) не трогаем.
        """
        result: list[str] = []
        last_end = 0
        for match in FILE_CONTENT_BLOCK_RE.finditer(text):
            if not match.group("body").strip().startswith(
                FILE_CONTENT_ERROR_PREFIX
            ):
                continue
            extracted = await self._extract_local_file_text(
                match.group("path").strip()
            )
            if extracted is None:
                continue
            result.append(text[last_end:match.start()])
            result.append(
                f'<file_content path="{match.group("path")}">\n'
                f"{extracted}\n"
                f"</file_content>"
            )
            last_end = match.end()
        if not result:
            return text
        result.append(text[last_end:])
        return "".join(result)

    async def _extract_local_file_text(self, path_str: str) -> Optional[str]:
        """
        Извлечь текст из локального файла, указанного в
        <file_content path="...">. Поддерживаются обычные пути и file:// URI,
        абсолютные пути и относительные (от корня workspace).

        Returns:
            Извлечённый текст или None, если извлечь не удалось (тогда
            исходный блок остаётся без изменений).
        """
        try:
            if path_str.lower().startswith("file://"):
                path_str = unquote(urlparse(path_str).path)
                # file:///C:/... -> C:/... (Windows)
                if re.match(r"^/[A-Za-z]:/", path_str):
                    path_str = path_str[1:]
            path = Path(path_str)
            if not path.is_absolute():
                path = (WORKSPACE_ROOT / path).resolve()
            if not path.is_file():
                logger.warning("Файл из <file_content> не найден: %s", path)
                return None
            if not FileParser.is_supported(path.name):
                logger.warning(
                    "Неподдерживаемый формат файла из <file_content>: %s", path
                )
                return None
            parsed = await self.file_parser.parse(path.read_bytes(), path.name)
            # Для review и облака используем markdown-представление (с таблицами)
            text = (parsed.markdown or parsed.text).strip()
            if not text:
                logger.warning("Из файла %s не извлечён текст", path)
                return None
            logger.info(
                "Извлечено %d символов из файла %s "
                "(клиент не смог прочитать его сам)",
                len(text), path,
            )
            return text
        except Exception as e:
            logger.error(
                "Не удалось извлечь содержимое файла %s: %s", path_str, e
            )
            return None

    async def _anonymize_messages(
        self,
        request: ChatCompletionRequest,
        session_id: str,
    ) -> tuple[list[dict], list[Entity], str, str]:
        """
        Общая логика: NER по склеенному тексту + анонимизация каждого
        сообщения с ПЕРЕСЧЁТОМ оффсетов (исправление бага мультисообщений).

        Returns:
            (anonymized_messages, entities, original_content, anonymized_content)
        """
        async def add_mapping(original_value: str, entity_type: str) -> str:
            return await self.store.add_mapping(session_id, original_value, entity_type)

        # В режиме anonymize_only системные сообщения исключаются из
        # анонимизации и результата: пользователю нужна только полезная часть
        # запроса (без системных промптов), и это ускоряет NER
        if request.mode == Mode.ANONYMIZE_ONLY:
            messages = [m for m in request.messages if m.role != "system"]
        else:
            messages = request.messages

        # Подменяем блоки <file_content>, которые клиент не смог прочитать
        # (бинарные документы DOCX/XLSX и т.п.), текстом, извлечённым прокси
        # из локального файла — иначе содержимое документа теряется
        messages = await self._resolve_failed_file_contents(messages)

        # Сегмент каждого сообщения = текст content + аргументы tool_calls
        segments, content_lens, args_meta_all = _collect_message_segments(
            messages
        )
        combined_text = "\n".join(segments)

        # NER выполняется ОДИН раз по склеенному тексту
        logger.info("NER для %d символов...", len(combined_text))
        entities, ner_time = await self.ner.extract_entities(combined_text, use_llm=True)
        logger.info("NER: %d сущностей за %.0fмс", len(entities), ner_time)

        # Разносим сущности по сегментам с локальными оффсетами
        segment_entities = split_entities_by_segments(entities, segments, separator="\n")

        # Анонимизируем каждое сообщение его собственными сущностями,
        # сохраняя служебные поля (name, tool_call_id, tool_calls)
        anonymized_messages = []
        for msg, seg_entities, content_len, args_meta in zip(
            messages, segment_entities, content_lens, args_meta_all
        ):
            # Сущности, относящиеся к content (оффсеты в пределах content_len)
            content_entities = [e for e in seg_entities if e.end <= content_len]
            anon_content, _ = await self.message_anonymizer.anonymize_message_content(
                msg.content, content_entities, add_mapping
            )

            anon_msg: dict = {"role": msg.role, "content": anon_content}
            if msg.name is not None:
                anon_msg["name"] = msg.name
            if msg.tool_call_id is not None:
                anon_msg["tool_call_id"] = msg.tool_call_id

            # Анонимизация аргументов tool_calls (структура вызовов сохраняется)
            if msg.tool_calls:
                anon_tool_calls = [
                    dict(tc) if isinstance(tc, dict) else tc
                    for tc in msg.tool_calls
                ]
                for tc_index, args_str, offset in args_meta:
                    if tc_index >= len(anon_tool_calls):
                        continue
                    tc = anon_tool_calls[tc_index]
                    if not isinstance(tc, dict):
                        continue
                    args_entities = [
                        Entity(
                            text=e.text,
                            type=e.type,
                            start=e.start - offset,
                            end=e.end - offset,
                            confidence=e.confidence,
                        )
                        for e in seg_entities
                        if offset <= e.start and e.end <= offset + len(args_str)
                    ]
                    anon_args, _ = await self.text_replacer.anonymize(
                        args_str, args_entities, add_mapping
                    )
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        tc["function"] = {**fn, "arguments": anon_args}
                anon_msg["tool_calls"] = anon_tool_calls

            anonymized_messages.append(anon_msg)

        original_content = json.dumps(
            [m.model_dump(exclude_none=True) for m in messages],
            ensure_ascii=False
        )
        anonymized_content = json.dumps(anonymized_messages, ensure_ascii=False)

        return anonymized_messages, entities, original_content, anonymized_content

    async def prepare_chat_request(
        self,
        request: ChatCompletionRequest,
        session_id: Optional[str] = None,
    ) -> PreparedRequest:
        """
        Подготовить запрос: NER + анонимизация.
        Вызывается ДО создания StreamingResponse, чтобы ошибки можно было
        перехватить и вернуть нормальный JSON error.
        """
        session_id = await self.store.get_or_create_session(session_id)

        anonymized_messages, entities, original_content, anonymized_content = (
            await self._anonymize_messages(request, session_id)
        )

        mappings_dict = await self.store.get_all_mappings(session_id)

        return PreparedRequest(
            session_id=session_id,
            anonymized_messages=anonymized_messages,
            mappings_dict=mappings_dict,
            entities=entities,
            original_content=original_content,
            anonymized_content=anonymized_content,
            canonical_result=_render_messages_markdown(anonymized_messages),
        )

    async def _save_anonymized_request(
        self,
        session_id: str,
        canonical_result: str,
    ):
        """
        Сохранить канонический анонимизированный результат в файл
        (data/anonymized_files/<session_id>/anonymized_request_*.md).
        Возвращает путь к файлу или None, если сохранение отключено/не удалось.
        """
        if not STORAGE["save_anonymized_files"]:
            return None
        try:
            return await self.store.save_anonymized_text(session_id, canonical_result)
        except Exception as e:
            logger.error("Не удалось сохранить анонимизированный файл: %s", e)
            return None

    def _build_summary_line(
        self,
        mode: str,
        session_id: str,
        saved_path,
        entities: list,
        mappings_dict: dict,
    ) -> str:
        """
        Одна служебная строка с минимумом техсведений о результате
        анонимизации (для ответа anonymize_only).
        """
        file_info = str(saved_path) if saved_path else "не сохранён"
        return (
            f"[Режим: {mode}; session_id: {session_id}; "
            f"файл: {file_info}; сущностей: {len(entities)}; "
            f"маппингов: {len(mappings_dict)}]"
        )

    async def _deanonymize_tool_calls(
        self,
        tool_calls: list[dict],
        mappings_dict: dict,
    ) -> list[dict]:
        """Де-анонимизировать аргументы tool_calls из ответа модели"""
        result = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                result.append(tc)
                continue
            fn = tc.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                deanonymized_args = await self.text_replacer.deanonymize(
                    fn["arguments"], mappings_dict
                )
                tc = {**tc, "function": {**fn, "arguments": deanonymized_args}}
            result.append(tc)
        return result

    async def handle_chat_completion(
        self,
        request: ChatCompletionRequest,
        session_id: Optional[str] = None
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Обработать запрос chat completion

        1. Анонимизировать сообщения
        2. Отправить в OpenRouter (если режим full)
        3. Де-анонимизировать ответ
        4. Записать лог

        Returns:
            Кортеж (ответ, session_id)
        """
        start_time = time.time()

        # Passthrough: anonymize=False или режим passthrough по умолчанию
        if not request.anonymize or CURRENT_MODE == Mode.PASSTHROUGH:
            return await self._handle_passthrough(request, session_id)

        # Переиспользуем prepare_chat_request (нет дублирования логики)
        prepared = await self.prepare_chat_request(request, session_id)
        session_id = prepared.session_id
        entities = prepared.entities
        mappings_dict = prepared.mappings_dict
        anonymized_messages = prepared.anonymized_messages
        original_content = prepared.original_content
        anonymized_content = prepared.anonymized_content
        canonical_result = prepared.canonical_result

        # В ОБОИХ режимах сохраняем канонический результат в файл:
        # то, что ушло в облако (full) или видит пользователь
        # (anonymize_only), всегда идентично содержимому файла
        saved_path = await self._save_anonymized_request(
            session_id, canonical_result
        )

        # Режим "только анонимизация" — не отправляем в облако
        if request.mode == Mode.ANONYMIZE_ONLY:
            processing_time = (time.time() - start_time) * 1000

            # Ответ: одна служебная строка техсведений + канонический
            # markdown-результат между маркерами (out-of-band техинформация
            # дублируется в anonymization_metadata)
            summary_line = self._build_summary_line(
                "anonymize_only", session_id, saved_path,
                entities, mappings_dict,
            )
            content = (
                f"{summary_line}\n\n"
                f"{RESULT_BEGIN}\n"
                f"{canonical_result}\n"
                f"{RESULT_END}"
            )

            # Создаём "ответ" с анонимизированным контентом
            response = ChatCompletionResponse(
                id=f"anonymize-only-{session_id}",
                created=int(time.time()),
                model=request.model,
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessage(
                            role="assistant",
                            content=content,
                        ),
                        finish_reason="stop"
                    )
                ],
                usage=UsageInfo(),
                anonymization_metadata={
                    "mode": "anonymize_only",
                    "session_id": session_id,
                    "anonymized_request_file": (
                        str(saved_path) if saved_path else None
                    ),
                    "entities_found": len(entities),
                    "mappings_count": len(mappings_dict),
                }
            )

            # Логируем
            await self.store.log_request(
                session_id=session_id,
                request_type="anonymize_only",
                original_content=original_content,
                anonymized_content=anonymized_content,
                response_content=None,
                entities_found=entities,
                processing_time_ms=processing_time
            )

            return response, session_id

        # Отправляем анонимизированные сообщения в OpenRouter
        final_messages = anonymized_messages

        # model=None → openrouter_client использует OPENROUTER["model"] из .env
        try:
            cloud_response = await self.openrouter.chat_completion(
                messages=final_messages,
                model=None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                n=request.n,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
                tools=request.tools,
                tool_choice=request.tool_choice,
                parallel_tool_calls=request.parallel_tool_calls,
            )

            # Де-анонимизируем ответ
            deanonymized_choices = []
            for choice in cloud_response.get("choices", []):
                message = choice.get("message", {})
                content = message.get("content")

                # Де-анонимизируем контент (может отсутствовать при tool_calls)
                if isinstance(content, str):
                    content = await self.text_replacer.deanonymize(content, mappings_dict)

                resp_message = ChatMessage(
                    role=message.get("role", "assistant"),
                    content=content
                )

                # Де-анонимизируем tool_calls модели (аргументы вызовов)
                if message.get("tool_calls"):
                    resp_message.tool_calls = await self._deanonymize_tool_calls(
                        message["tool_calls"], mappings_dict
                    )

                deanonymized_choices.append(
                    ChatCompletionChoice(
                        index=choice.get("index", 0),
                        message=resp_message,
                        finish_reason=choice.get("finish_reason")
                    )
                )

            processing_time = (time.time() - start_time) * 1000

            # Формируем финальный ответ
            response = ChatCompletionResponse(
                id=cloud_response.get("id", f"anon-{session_id}"),
                created=cloud_response.get("created", int(time.time())),
                model=cloud_response.get("model", request.model),
                choices=deanonymized_choices,
                usage=UsageInfo(**cloud_response.get("usage", {})),
                anonymization_metadata={
                    "mode": request.mode,
                    "session_id": session_id,
                    "anonymized_request_file": (
                        str(saved_path) if saved_path else None
                    ),
                    "entities_found": len(entities),
                    "mappings_count": len(mappings_dict),
                    "processing_time_ms": processing_time
                }
            )

            # Логируем
            response_content = json.dumps(
                [c.model_dump() for c in deanonymized_choices],
                ensure_ascii=False
            )

            await self.store.log_request(
                session_id=session_id,
                request_type="chat_completion",
                original_content=original_content,
                anonymized_content=anonymized_content,
                response_content=response_content,
                entities_found=entities,
                processing_time_ms=processing_time
            )

            return response, session_id

        except Exception as e:
            processing_time = (time.time() - start_time) * 1000

            # Логируем ошибку
            await self.store.log_request(
                session_id=session_id,
                request_type="chat_completion",
                original_content=original_content,
                anonymized_content=anonymized_content,
                response_content=None,
                entities_found=entities,
                processing_time_ms=processing_time,
                error=str(e)
            )

            raise

    async def _stream_anonymize_only(
        self,
        request: ChatCompletionRequest,
        prepared: PreparedRequest,
        start_time: float,
    ) -> AsyncIterator[str]:
        """SSE-поток для режима anonymize_only"""
        session_id = prepared.session_id
        response_id = f"anonymize-only-{session_id}"
        created = int(time.time())

        # Сохраняем канонический анонимизированный результат в файл
        saved_path = await self._save_anonymized_request(
            session_id, prepared.canonical_result
        )

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        # Стрим: служебная строка техсведений + канонический markdown
        # между маркерами; техинформация дублируется в anonymization_metadata
        # финального чанка
        summary_line = self._build_summary_line(
            "anonymize_only", session_id, saved_path,
            prepared.entities, prepared.mappings_dict,
        )
        anon_text = (
            f"{summary_line}\n\n"
            f"{RESULT_BEGIN}\n"
            f"{prepared.canonical_result}\n"
            f"{RESULT_END}"
        )
        chunk_size = 50
        for i in range(0, len(anon_text), chunk_size):
            text_chunk = anon_text[i:i + chunk_size]
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "anonymization_metadata": {
                "mode": "anonymize_only",
                "session_id": session_id,
                "anonymized_request_file": (
                    str(saved_path) if saved_path else None
                ),
                "entities_found": len(prepared.entities),
                "mappings_count": len(prepared.mappings_dict),
            },
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

        processing_time = (time.time() - start_time) * 1000
        await self.store.log_request(
            session_id=session_id,
            request_type="anonymize_only_stream",
            original_content=prepared.original_content,
            anonymized_content=prepared.anonymized_content,
            response_content=None,
            entities_found=prepared.entities,
            processing_time_ms=processing_time
        )

    async def stream_from_prepared(
        self,
        request: ChatCompletionRequest,
        prepared: PreparedRequest,
    ) -> AsyncIterator[str]:
        """
        Стримить ответ, используя уже подготовленные данные (NER + анонимизация).
        Вызывается ПОСЛЕ успешного prepare_chat_request.

        Де-анонимизация выполняется через StreamDeAnonymizer с буферизацией:
        разорванные на границах чанков токены не утекают к клиенту.
        """
        start_time = time.time()

        # Режим "только анонимизация"
        if request.mode == Mode.ANONYMIZE_ONLY:
            async for event in self._stream_anonymize_only(
                request,
                prepared,
                start_time,
            ):
                yield event
            return

        # Режим "full" — стримим от OpenRouter.
        # Перед отправкой в облако сохраняем канонический результат в файл:
        # содержимое файла — проверяемая проекция контекста, который
        # реально уходит в OpenRouter (prepared.anonymized_messages)
        saved_path = await self._save_anonymized_request(
            prepared.session_id, prepared.canonical_result
        )
        try:
            final_messages = prepared.anonymized_messages
            collected_content = ""
            deano = StreamDeAnonymizer(self.text_replacer, prepared.mappings_dict)
            # Отдельные буферы де-анонимизации для аргументов каждого tool_call
            # (ключ: (choice_index, tool_call_index))
            arg_deanos: dict[tuple[int, int], StreamDeAnonymizer] = {}

            async for chunk in self.openrouter.chat_completion_stream(
                messages=final_messages,
                model=None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                n=request.n,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
                tools=request.tools,
                tool_choice=request.tool_choice,
                parallel_tool_calls=request.parallel_tool_calls,
            ):
                choices = chunk.get("choices", [])
                for choice in choices:
                    delta = choice.get("delta", {})
                    if "content" in delta and delta["content"]:
                        # Буферизованная де-анонимизация
                        deanonymized = await deano.feed(delta["content"])
                        collected_content += deanonymized
                        if deanonymized:
                            delta["content"] = deanonymized
                        else:
                            # Весь фрагмент удержан в буфере — пустой контент
                            delta["content"] = ""

                    # Буферизованная де-анонимизация аргументов tool_calls:
                    # плейсхолдер может быть разорван на границе чанков
                    for tc_delta in delta.get("tool_calls") or []:
                        if not isinstance(tc_delta, dict):
                            continue
                        tc_index = tc_delta.get("index", 0)
                        fn = tc_delta.get("function")
                        if not isinstance(fn, dict):
                            continue
                        args_frag = fn.get("arguments")
                        if isinstance(args_frag, str) and args_frag:
                            key = (choice.get("index", 0), tc_index)
                            if key not in arg_deanos:
                                arg_deanos[key] = StreamDeAnonymizer(
                                    self.text_replacer, prepared.mappings_dict
                                )
                            fn["arguments"] = await arg_deanos[key].feed(args_frag)

                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            # Сбрасываем остаток буфера
            tail = await deano.flush()
            if tail:
                collected_content += tail
                tail_chunk = {
                    "id": f"anon-{prepared.session_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": request.model,
                    "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

            # Сбрасываем остатки буферов аргументов tool_calls
            for (choice_idx, tc_index), tc_deano in arg_deanos.items():
                tail_args = await tc_deano.flush()
                if tail_args:
                    args_tail_chunk = {
                        "id": f"anon-{prepared.session_id}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [{
                            "index": choice_idx,
                            "delta": {"tool_calls": [{
                                "index": tc_index,
                                "function": {"arguments": tail_args}
                            }]},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(args_tail_chunk, ensure_ascii=False)}\n\n"

            processing_time = (time.time() - start_time) * 1000

            # Финальный control-чанк с метаданными анонимизации (ДО [DONE],
            # иначе клиенты его не прочитают; delta пустой — потребители SSE,
            # ориентирующиеся на OpenAI-формат, его безопасно игнорируют)
            metadata_chunk = {
                "id": f"anon-{prepared.session_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                "anonymization_metadata": {
                    "mode": request.mode,
                    "session_id": prepared.session_id,
                    "anonymized_request_file": (
                        str(saved_path) if saved_path else None
                    ),
                    "entities_found": len(prepared.entities),
                    "mappings_count": len(prepared.mappings_dict),
                    "processing_time_ms": processing_time,
                },
            }
            yield f"data: {json.dumps(metadata_chunk, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"

            await self.store.log_request(
                session_id=prepared.session_id,
                request_type="chat_completion_stream",
                original_content=prepared.original_content,
                anonymized_content=prepared.anonymized_content,
                response_content=collected_content,
                entities_found=prepared.entities,
                processing_time_ms=processing_time
            )

        except Exception as e:
            processing_time = (time.time() - start_time) * 1000
            await self.store.log_request(
                session_id=prepared.session_id,
                request_type="chat_completion_stream",
                original_content=prepared.original_content,
                anonymized_content=prepared.anonymized_content,
                response_content=None,
                entities_found=prepared.entities,
                processing_time_ms=processing_time,
                error=str(e)
            )
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    async def _handle_passthrough(
        self,
        request: ChatCompletionRequest,
        session_id: Optional[str] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Passthrough-режим (anonymize=False): отправить запрос в OpenRouter
        БЕЗ анонимизации и де-анонимизации (обычный прокси).
        """
        messages_payload = [
            m.model_dump(exclude_none=True) for m in request.messages
        ]

        cloud_response = await self.openrouter.chat_completion(
            messages=messages_payload,
            model=None,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            top_p=request.top_p,
            n=request.n,
            stop=request.stop,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
        )

        choices = []
        for choice in cloud_response.get("choices", []):
            message_data = choice.get("message", {})
            resp_message = ChatMessage(
                role=message_data.get("role", "assistant"),
                content=message_data.get("content"),
            )
            if message_data.get("tool_calls"):
                resp_message.tool_calls = message_data["tool_calls"]
            if message_data.get("name"):
                resp_message.name = message_data["name"]
            if message_data.get("tool_call_id"):
                resp_message.tool_call_id = message_data["tool_call_id"]
            choices.append(
                ChatCompletionChoice(
                    index=choice.get("index", 0),
                    message=resp_message,
                    finish_reason=choice.get("finish_reason"),
                )
            )

        response = ChatCompletionResponse(
            id=cloud_response.get("id", f"passthrough-{int(time.time())}"),
            created=cloud_response.get("created", int(time.time())),
            model=cloud_response.get("model", request.model),
            choices=choices,
            usage=UsageInfo(**cloud_response.get("usage", {})),
            anonymization_metadata={"mode": "passthrough"},
        )
        return response, session_id or "passthrough"

    async def stream_passthrough(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[str]:
        """
        Стриминг passthrough (anonymize=False): без анонимизации и
        де-анонимизации (обычный прокси-стрим).
        """
        messages_payload = [
            m.model_dump(exclude_none=True) for m in request.messages
        ]

        try:
            async for chunk in self.openrouter.chat_completion_stream(
                messages=messages_payload,
                model=None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                n=request.n,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
                tools=request.tools,
                tool_choice=request.tool_choice,
                parallel_tool_calls=request.parallel_tool_calls,
            ):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    async def handle_send_anonymized(
        self,
        request: SendAnonymizedRequest,
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Отправить анонимизированный промпт в облако и де-анонимизировать ответ.

        content отправляется как единое user-сообщение; ответ де-анонимизируется
        по маппингам session_id.
        """
        mappings_dict = await self.store.get_all_mappings(request.session_id)
        messages = [{"role": "user", "content": request.content}]

        cloud_response = await self.openrouter.chat_completion(
            messages=messages,
            model=None,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        deanonymized_choices = []
        for choice in cloud_response.get("choices", []):
            message = choice.get("message", {})
            content = message.get("content")
            if isinstance(content, str):
                content = await self.text_replacer.deanonymize(content, mappings_dict)

            resp_message = ChatMessage(
                role=message.get("role", "assistant"),
                content=content,
            )
            if message.get("tool_calls"):
                resp_message.tool_calls = await self._deanonymize_tool_calls(
                    message["tool_calls"], mappings_dict
                )
            deanonymized_choices.append(
                ChatCompletionChoice(
                    index=choice.get("index", 0),
                    message=resp_message,
                    finish_reason=choice.get("finish_reason"),
                )
            )

        response = ChatCompletionResponse(
            id=cloud_response.get("id", f"send-{request.session_id}"),
            created=cloud_response.get("created", int(time.time())),
            model=cloud_response.get("model", "openrouter"),
            choices=deanonymized_choices,
            usage=UsageInfo(**cloud_response.get("usage", {})),
            anonymization_metadata={
                "mode": "send",
                "session_id": request.session_id,
                "mappings_count": len(mappings_dict),
            },
        )
        return response, request.session_id

    async def stream_send_anonymized(
        self,
        request: SendAnonymizedRequest,
    ) -> AsyncIterator[str]:
        """Стриминг: отправить анонимизированный промпт и де-анонимизировать ответ."""
        mappings_dict = await self.store.get_all_mappings(request.session_id)
        messages = [{"role": "user", "content": request.content}]

        collected_content = ""
        deano = StreamDeAnonymizer(self.text_replacer, mappings_dict)
        arg_deanos: dict[tuple[int, int], StreamDeAnonymizer] = {}

        try:
            async for chunk in self.openrouter.chat_completion_stream(
                messages=messages,
                model=None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            ):
                choices = chunk.get("choices", [])
                for choice in choices:
                    delta = choice.get("delta", {})
                    if "content" in delta and delta["content"]:
                        deanonymized = await deano.feed(delta["content"])
                        collected_content += deanonymized
                        delta["content"] = deanonymized if deanonymized else ""

                    for tc_delta in delta.get("tool_calls") or []:
                        if not isinstance(tc_delta, dict):
                            continue
                        tc_index = tc_delta.get("index", 0)
                        fn = tc_delta.get("function")
                        if not isinstance(fn, dict):
                            continue
                        args_frag = fn.get("arguments")
                        if isinstance(args_frag, str) and args_frag:
                            key = (choice.get("index", 0), tc_index)
                            if key not in arg_deanos:
                                arg_deanos[key] = StreamDeAnonymizer(
                                    self.text_replacer, mappings_dict
                                )
                            fn["arguments"] = await arg_deanos[key].feed(args_frag)

                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            tail = await deano.flush()
            if tail:
                collected_content += tail
                tail_chunk = {
                    "id": f"send-{request.session_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "openrouter",
                    "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

            for (choice_idx, tc_index), tc_deano in arg_deanos.items():
                tail_args = await tc_deano.flush()
                if tail_args:
                    args_tail_chunk = {
                        "id": f"send-{request.session_id}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "openrouter",
                        "choices": [{
                            "index": choice_idx,
                            "delta": {"tool_calls": [{"index": tc_index, "function": {"arguments": tail_args}}]},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(args_tail_chunk, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    async def handle_anonymize_only(
        self,
        request: AnonymizeRequest
    ) -> AnonymizeResponse:
        """
        Обработать запрос "только анонимизация"
        Поддерживает текст и файлы
        """
        start_time = time.time()

        # Получаем или создаём сессию
        session_id = await self.store.get_or_create_session(request.session_id)

        # Функция для добавления маппинга
        async def add_mapping(original_value: str, entity_type: str) -> str:
            return await self.store.add_mapping(session_id, original_value, entity_type)

        all_entities = []
        anonymized_text = None
        anonymized_files = []
        storage_paths = []

        # Обрабатываем текст
        if request.text:
            entities, _ = await self.ner.extract_entities(request.text, use_llm=True)
            all_entities.extend(entities)

            anonymized_text, _ = await self.text_replacer.anonymize(
                request.text, entities, add_mapping
            )

        # Обрабатываем файлы
        if request.files:
            for file_info in request.files:
                filename = file_info.get("filename", "unknown")
                content_b64 = file_info.get("content")  # base64 encoded

                if not content_b64:
                    continue

                try:
                    content = base64.b64decode(content_b64)

                    # Парсим файл
                    parsed = await self.file_parser.parse(content, filename)

                    # Извлекаем сущности
                    entities, _ = await self.ner.extract_entities(parsed.text, use_llm=True)
                    all_entities.extend(entities)

                    # Анонимизируем
                    anon_text, _ = await self.text_replacer.anonymize(
                        parsed.text, entities, add_mapping
                    )

                    # Собираем анонимизированный файл
                    anon_content = await self.file_assembler.assemble(
                        content, filename, anon_text, parsed.structure
                    )

                    # Сохраняем файлы для оценки качества
                    if STORAGE["save_anonymized_files"]:
                        original_path, anon_path = await self.store.save_anonymized_file(
                            session_id, filename, content, anon_content
                        )
                        storage_paths.append({
                            "original": str(original_path),
                            "anonymized": str(anon_path)
                        })

                    # Кодируем обратно в base64
                    anon_b64 = base64.b64encode(anon_content).decode("utf-8")

                    anonymized_files.append({
                        "filename": filename,
                        "content": anon_b64,
                        "content_type": FileParser.get_content_type(filename),
                        "entities_found": len(entities)
                    })

                except Exception as e:
                    logger.error("Ошибка обработки файла %s: %s", filename, e)
                    anonymized_files.append({
                        "filename": filename,
                        "error": str(e)
                    })

        processing_time = (time.time() - start_time) * 1000

        # Получаем количество маппингов
        mappings = await self.store.get_all_mappings(session_id)

        # Логируем
        await self.store.log_request(
            session_id=session_id,
            request_type="anonymize_only",
            original_content=request.text or "",
            anonymized_content=anonymized_text or "",
            response_content=json.dumps({"files": len(anonymized_files)}, ensure_ascii=False),
            entities_found=all_entities,
            processing_time_ms=processing_time
        )

        return AnonymizeResponse(
            session_id=session_id,
            anonymized_text=anonymized_text,
            anonymized_files=anonymized_files if anonymized_files else None,
            entities_found=all_entities,
            mappings_count=len(mappings),
            processing_time_ms=processing_time,
            storage_path=json.dumps(storage_paths) if storage_paths else None
        )

    async def handle_deanonymize(
        self,
        text: str,
        session_id: str
    ) -> str:
        """Де-анонимизировать текст по session_id"""
        mappings = await self.store.get_all_mappings(session_id)
        return await self.text_replacer.deanonymize(text, mappings)

    async def handle_deanonymize_file(
        self,
        session_id: str,
        file_path: str,
        output_path: Optional[str] = None,
    ) -> dict:
        """
        Де-анонимизировать файл: заменить плейсхолдеры на реальные значения
        из маппингов сессии и пересохранить файл (структура/таблицы сохраняются).
        """
        mappings_dict = await self.store.get_all_mappings(session_id)
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")

        target = Path(output_path) if output_path else path
        ext = path.suffix.lower()

        if ext in (".txt", ".md"):
            text = path.read_text(encoding="utf-8")
            deanonymized = await self.text_replacer.deanonymize(text, mappings_dict)
            target.write_text(deanonymized, encoding="utf-8")
        elif ext in (".docx", ".xlsx", ".xml"):
            original = path.read_bytes()
            parsed = await self.file_parser.parse(original, path.name)
            deanonymized_text = await self.text_replacer.deanonymize(
                parsed.text, mappings_dict
            )
            assembled = await self.file_assembler.assemble(
                original, path.name, deanonymized_text, parsed.structure
            )
            target.write_bytes(assembled)
        else:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}")

        return {
            "session_id": session_id,
            "file_path": str(target),
            "mappings_count": len(mappings_dict),
        }

    async def handle_anonymize_file(
        self,
        file_path: str,
        session_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> dict:
        """
        Анонимизировать локальный файл: создать анонимизированную копию
        (структура/таблицы сохраняются) рядом с оригиналом.

        Возвращает путь к анонимизированной копии — модель (Cline) редактирует
        именно её, а в конце де-анонимизация через /api/deanonymize_file.
        """
        session_id = await self.store.get_or_create_session(session_id)
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        if not FileParser.is_supported(path.name):
            raise ValueError(f"Неподдерживаемый формат файла: {path.name}")

        content = path.read_bytes()
        parsed = await self.file_parser.parse(content, path.name)

        async def add_mapping(original_value: str, entity_type: str) -> str:
            return await self.store.add_mapping(
                session_id, original_value, entity_type
            )

        entities, _ = await self.ner.extract_entities(parsed.text, use_llm=True)

        # Маппинг value -> token для анонимизации markdown-представления
        value_to_token: dict[str, str] = {}
        for e in entities:
            if e.text not in value_to_token:
                value_to_token[e.text] = await add_mapping(e.text, e.type)

        anon_text, _ = await self.text_replacer.anonymize(
            parsed.text, entities, add_mapping
        )
        anon_content = await self.file_assembler.assemble(
            content, path.name, anon_text, parsed.structure
        )

        # Анонимизированный markdown (с таблицами) для review
        anon_markdown = self.text_replacer.replace_by_value(
            parsed.markdown or parsed.text, value_to_token
        )

        if output_path:
            target = Path(output_path)
        else:
            target = path.with_name(f"{path.stem}.anonymized{path.suffix}")
        target.write_bytes(anon_content)

        # Сохраняем review-файл (.md) с форматированным анонимизированным видом
        review_path = None
        if anon_markdown and STORAGE["save_anonymized_files"]:
            try:
                review_path = await self.store.save_anonymized_text(
                    session_id, anon_markdown, prefix="review"
                )
            except Exception as e:
                logger.error("Не удалось сохранить review-файл: %s", e)

        mappings = await self.store.get_all_mappings(session_id)
        return {
            "session_id": session_id,
            "original_file": str(path),
            "anonymized_file": str(target),
            "review_file": str(review_path) if review_path else None,
            "anonymized_markdown": anon_markdown,
            "entities_found": len(entities),
            "mappings_count": len(mappings),
        }

    async def handle_get_sessions(self) -> list[dict]:
        """Получить список активных сессий."""
        return await self.store.get_all_sessions()

    async def get_session_logs(
        self,
        session_id: str,
        limit: int = 100
    ) -> list[dict]:
        """Получить логи сессии"""
        return await self.store.get_logs(session_id, limit)

    async def get_all_logs(self, limit: int = 100) -> list[dict]:
        """Получить все логи"""
        return await self.store.get_logs(limit=limit)

    async def close(self):
        """Закрыть все соединения"""
        await self.ner.close()
        await self.openrouter.close()
        await self.store.close()