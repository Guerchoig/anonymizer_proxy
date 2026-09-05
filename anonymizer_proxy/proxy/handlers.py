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

from ..anonymizer.ner_service import NERService, NERUnavailableError
from ..anonymizer.file_parser import FileParser, FileAssembler
from ..anonymizer.mapping_store import MappingStore
from ..anonymizer.replacer import (
    TextReplacer,
    MessageAnonymizer,
    StreamDeAnonymizer,
    split_entities_by_segments,
)
from ..config import (
    Mode, CURRENT_MODE, STORAGE, RESULT_BEGIN, RESULT_END, LOCAL_LLM,
    COMMAND_CLASSIFIER, acting_cloud_provider, CLOUD_PROVIDERS,
)
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
from .command_classifier import looks_like_command, classify_command_intent
from .openrouter_client import OpenRouterClient
from .utils import (
    FILE_CONTENT_BLOCK_RE,
    FILE_CONTENT_ERROR_PREFIX,
    OFFICE_PATH_RE,
    ATTACHMENT_FILE_REF_RE,
    BACKTICKED_OFFICE_PATH_RE,
    PATH_MENTION_RE,
    WORKSPACE_ROOT,
    ANONYMIZE_INTENT_RE,
    ANONYMIZER_DONE_MARKER_RE,
    SESSION_ID_LINE_RE,
    DEANONYMIZE_INTENT_RE,
    RESTART_INTENT_RE,
    LOCAL_BACKEND_RE,
    CLOUD_BACKEND_RE,
    COMMAND_TRIGGER_RE,
    ANONYMIZER_COPY_MARKER_RE,
    ANONYMIZER_RESULT_MARKER_RE,
    _iter_content_texts,
    _resolve_local_path,
    _is_anonymized_copy_path,
    _result_path_for,
    last_user_message_texts,
    norm_fs_path,
)

logger = logging.getLogger("anonymizer_proxy.handlers")


class PreparedFilesAnonymization:
    """
    Результат автоматической анонимизации приложенных файлов
    (passthrough-режим): файлы уже обработаны локальной NER-моделью,
    осталось только отдать клиенту текстовый ответ (стримом или нет).
    """

    def __init__(
        self,
        session_id: str,
        response_text: str,
        files: list[dict],
        entities_total: int,
        mappings_count: int,
        original_content: str,
        start_time: float,
    ):
        self.session_id = session_id
        self.response_text = response_text
        self.files = files
        self.entities_total = entities_total
        self.mappings_count = mappings_count
        self.original_content = original_content
        self.start_time = start_time


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
            path = _resolve_local_path(path_str)
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

    async def _substitute_anonymized_file_blocks(self, text: str) -> str:
        """
        Заменить тела неудачно прочитанных клиентом <file_content>-блоков,
        если путь указывает на анонимизированную копию (<name>.anonymized.<ext>),
        текстом, извлечённым прокси из локального файла.

        Обычные (не анонимизированные) файлы не трогаем — контракт passthrough:
        их содержимое не должно самопроизвольно попадать в облако.
        """
        result: list[str] = []
        last_end = 0
        for match in FILE_CONTENT_BLOCK_RE.finditer(text):
            raw_path = match.group("path").strip()
            if not match.group("body").strip().startswith(FILE_CONTENT_ERROR_PREFIX):
                continue
            if not _is_anonymized_copy_path(raw_path):
                continue
            extracted = await self._extract_local_file_text(raw_path)
            if extracted is None:
                continue
            result.append(text[last_end:match.start()])
            result.append(
                f'<file_content path="{raw_path}">\n'
                f"{extracted}\n"
                f"</file_content>"
            )
            last_end = match.end()
        if not result:
            return text
        result.append(text[last_end:])
        return "".join(result)

    async def _resolve_anonymized_file_contents(
        self,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        """
        Подменить тела блоков <file_content> для анонимизированных копий
        (клиент не умеет читать бинарные DOCX/XLSX и присылает заглушку
        «Error fetching content»). Облако получает извлечённый текст.
        """
        resolved: list[ChatMessage] = []
        for msg in messages:
            if isinstance(msg.content, str) and FILE_CONTENT_ERROR_PREFIX in msg.content:
                msg = msg.model_copy(update={
                    "content": await self._substitute_anonymized_file_blocks(msg.content)
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
                            "text": await self._substitute_anonymized_file_blocks(
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

    # ==================== Авто-анонимизация приложенных файлов ====================

    def detect_attached_files_anonymization(
        self,
        request: ChatCompletionRequest,
    ) -> list[str]:
        """
        Определить, нужно ли перехватить запрос для автоматической анонимизации
        приложенных файлов (только passthrough-режим).

        Условия перехвата:
        1. В ТЕКУЩЕМ (последнем содержательном) user-сообщении есть команда
           анонимизации («анонимиз…» / «anonymis/z…»). Команда из старых
           сообщений истории не считается: она остаётся там навсегда и без
           этого ограничения ложно перехватывает последующие запросы
           («сравни два файла», «составь отчёт» и т.п.);
        2. В сообщениях есть блоки <file_content path="..."> с путями к файлам,
           которые ещё НЕ анонимизированы в этом диалоге (нет маркера
           [anonymizer:done:<путь>] в истории) и не являются анонимизированными
           копиями (<name>.anonymized.<ext>).

        Returns:
            Список абсолютных путей файлов для анонимизации
            (пустой список — запрос обрабатывается как обычно).
        """
        has_intent = any(
            ANONYMIZE_INTENT_RE.search(text)
            for text in last_user_message_texts(request.messages)
        )
        if not has_intent:
            return []

        candidates: list[str] = []
        done: set[str] = set()
        for msg in request.messages:
            # Системный промпт (Hermes/Cline) — НЕ источник файлов для
            # анонимизации. Он всегда упоминает служебные файлы агента
            # (AGENTS.md и т.п.): реальный кейс 2026-09-05 — «анонимизируй
            # приложенные файлы» анонимизировал AGENTS.md из корня проекта
            # вместо приложенных docx, потому что путь AGENTS.md попал в
            # кандидаты из системного промпта.
            if (msg.role or "").lower() == "system":
                continue
            for text in _iter_content_texts(msg.content):
                for match in FILE_CONTENT_BLOCK_RE.finditer(text):
                    raw_path = match.group("path").strip()
                    if raw_path not in candidates:
                        candidates.append(raw_path)
                # Ссылки вида «@file:<путь>» — формат вложений клиента Hermes
                for match in ATTACHMENT_FILE_REF_RE.finditer(text):
                    raw_path = (match.group("path").strip()
                                .strip('"').strip("'").strip("`"))
                    if not raw_path or raw_path in candidates:
                        continue
                    if not FileParser.is_supported(raw_path):
                        # Hermes может приложить что угодно (картинки, PDF) —
                        # анонимизируем только поддерживаемые форматы
                        continue
                    candidates.append(raw_path)
                # Путь в бэктиках с офисным расширением — блок «Attached
                # Context» Hermes (абсолютный путь с пробелами, \S+ его не
                # берёт): …available on disk at `C:\…\Имя с пробелами.docx`
                for match in BACKTICKED_OFFICE_PATH_RE.finditer(text):
                    raw_path = match.group(1).strip()
                    if raw_path and raw_path not in candidates:
                        candidates.append(raw_path)
                # Пути к файлам, упомянутые в тексте сообщения
                # («анонимизируй файл c:\docs\KP_IRIS.docx»)
                for match in PATH_MENTION_RE.finditer(text):
                    raw_path = match.group(0).strip()
                    if raw_path and raw_path not in candidates:
                        candidates.append(raw_path)
                for match in ANONYMIZER_DONE_MARKER_RE.finditer(text):
                    # Сравнение путей — по канонической форме (слэши/регистр),
                    # иначе маркер «не узнаёт» тот же путь в другой записи
                    done.add(norm_fs_path(match.group("path").strip()))

        result: list[str] = []
        for raw_path in candidates:
            try:
                resolved = _resolve_local_path(raw_path)
            except Exception:
                continue
            resolved_str = str(resolved)
            # Упомянутый пути файл обязан существовать: в тексте могут быть
            # гипотетические/опечатанные пути, в облако такие запросы не перехватываем
            if not resolved.is_file():
                continue
            # Анонимизированная копия (<name>.anonymized.<ext>) повторно
            # не обрабатывается
            if resolved.stem.lower().endswith(".anonymized"):
                continue
            if norm_fs_path(raw_path) in done:
                continue
            if resolved_str in result:
                continue
            result.append(resolved_str)
        return result

    async def _history_anonymization_mappings(
        self,
        request: ChatCompletionRequest,
    ) -> tuple[dict[str, str], list[str]]:
        """
        Собрать маппинги (token -> original_value) сессий анонимизации,
        упомянутых в истории диалога.

        Ищет маркеры [ANONYMIZER] / [anonymizer:done:...] в сообщениях и
        извлекает session_id из строки «session_id: <uuid>» рядом с ними.
        Возвращает объединённый словарь маппингов (при конфликте токенов
        побеждает более поздняя сессия) и список найденных session_id.
        Пустой словарь — анонимизация в диалоге не выполнялась.
        """
        session_ids: list[str] = []
        for msg in request.messages:
            for text in _iter_content_texts(msg.content):
                if "[ANONYMIZER]" not in text and "[anonymizer:done:" not in text:
                    continue
                for match in SESSION_ID_LINE_RE.finditer(text):
                    sid = match.group(1)
                    if sid not in session_ids:
                        session_ids.append(sid)

        if not session_ids:
            return {}, []

        merged: dict[str, str] = {}
        for sid in session_ids:
            try:
                mappings = await self.store.get_all_mappings(sid)
            except Exception:
                continue
            if mappings:
                merged.update(mappings)
        return merged, session_ids

    async def prepare_files_anonymization(
        self,
        request: ChatCompletionRequest,
        file_paths: list[str],
        session_id: Optional[str] = None,
    ) -> PreparedFilesAnonymization:
        """
        Анонимизировать приложенные файлы локальной NER-моделью (без облака).

        Для каждого файла создаётся анонимизированная копия
        <name>.anonymized.<ext> рядом с оригиналом (пользователь правит именно
        её). Все файлы одного запроса используют ОДНУ сессию — одна и та же PII
        получает один и тот же плейсхолдер во всех файлах.
        """
        start_time = time.time()
        session_id = await self.store.get_or_create_session(session_id)

        files_info: list[dict] = []
        entities_total = 0
        for file_path in file_paths:
            try:
                result = await self.handle_anonymize_file(
                    file_path, session_id=session_id, create_review_md=False
                )
                files_info.append({
                    "original_file": result["original_file"],
                    "anonymized_file": result["anonymized_file"],
                    "entities_found": result["entities_found"],
                })
                # Связь файл → сессия (частичная де-анонимизация колонок)
                anon_path = Path(result["anonymized_file"])
                result_derived = Path(_result_path_for(str(anon_path)))
                for reg_path in (Path(result["original_file"]), anon_path,
                                 result_derived):
                    await self.store.register_file_session(
                        str(reg_path), session_id)
                entities_total += result["entities_found"]
                logger.info(
                    "Файл %s анонимизирован → %s (сущностей: %d)",
                    file_path, result["anonymized_file"], result["entities_found"],
                )
            except (FileNotFoundError, ValueError) as e:
                logger.warning(
                    "Не удалось анонимизировать файл %s: %s", file_path, e
                )
                files_info.append({"original_file": file_path, "error": str(e)})

        mappings_dict = await self.store.get_all_mappings(session_id)
        response_text = self._build_files_anonymization_text(
            session_id, files_info
        )
        original_content = json.dumps(
            [m.model_dump(exclude_none=True) for m in request.messages],
            ensure_ascii=False,
        )
        return PreparedFilesAnonymization(
            session_id=session_id,
            response_text=response_text,
            files=files_info,
            entities_total=entities_total,
            mappings_count=len(mappings_dict),
            original_content=original_content,
            start_time=start_time,
        )

    @staticmethod
    def _build_files_anonymization_text(
        session_id: str,
        files_info: list[dict],
    ) -> str:
        """
        Текст ответа клиенту после автоматической анонимизации файлов.

        Машиночитаемые маркеры:
        - [anonymizer:done:<путь>] — файл уже анонимизирован (защита от
          повторного перехвата при следующих запросах агента);
        - [anonymizer:copy:<путь>] — путь к анонимизированной копии;
        - [anonymizer:result:<путь>] — путь к файлу результата (куда модель
          пишет правки; де-анонимизируется в конце).
        """
        lines = [
            "[ANONYMIZER] Приложенные файлы анонимизированы локальной "
            "NER-моделью. Запрос в облако НЕ отправлялся (режим passthrough).",
            "",
            f"session_id: {session_id}",
            "",
            "Файлы:",
        ]
        for info in files_info:
            if "error" in info:
                lines.append(f"- {info['original_file']} — ОШИБКА: {info['error']}")
                continue
            result_file = _result_path_for(info["anonymized_file"])
            lines.append(
                f"- {info['original_file']} → {info['anonymized_file']} "
                f"(сущностей: {info['entities_found']})"
            )
            lines.append(f"  [anonymizer:done:{info['original_file']}]")
            lines.append(f"  [anonymizer:copy:{info['anonymized_file']}]")
            lines.append(f"  [anonymizer:result:{result_file}]")
        lines += [
            "",
            "Дальнейшие шаги:",
            "1. Проверьте анонимизированную копию (<name>.anonymized.<ext>) — "
            "PII заменены плейсхолдерами.",
            "2. Чтобы обработать документ в облаке — просто напишите задачу "
            "обычным сообщением (например, «добавь колонку в таблицы», "
            "«составь резюме»). Отдельная команда отправки не нужна.",
            "3. ОБЯЗАТЕЛЬНО при изменении документа — правило цепочки правок: "
            "ПЕРВАЯ правка — --file <анонимизированная копия> --output "
            "<файл результата> (путь в маркере [anonymizer:result:<путь>] "
            "выше). ВСЕ ПОСЛЕДУЮЩИЕ правки — только с --file <файл "
            "результата> и записью в него же (--output <файл результата> "
            "или --in-place): команда, начатая заново с копии, ЗАТРЁТ "
            "предыдущие правки. Анонимизированную копию не изменяйте — "
            "она исходник только для чтения (dump/list-tables).",
            "4. В конце — де-анонимизация: напишите «деанонимизируй "
            "упомянутые файлы».",
        ]
        return "\n".join(lines)

    async def handle_files_anonymization(
        self,
        request: ChatCompletionRequest,
        file_paths: list[str],
        session_id: Optional[str] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """Не-стриминговый ответ после автоматической анонимизации файлов"""
        prepared = await self.prepare_files_anonymization(
            request, file_paths, session_id
        )
        processing_time = (time.time() - prepared.start_time) * 1000
        response = ChatCompletionResponse(
            id=f"files-anonymized-{prepared.session_id}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=prepared.response_text,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=UsageInfo(),
            anonymization_metadata={
                "mode": "files_anonymization",
                "session_id": prepared.session_id,
                "files": prepared.files,
                "entities_found": prepared.entities_total,
                "mappings_count": prepared.mappings_count,
                "processing_time_ms": processing_time,
            },
        )
        await self.store.log_request(
            session_id=prepared.session_id,
            request_type="files_anonymization",
            original_content=prepared.original_content,
            anonymized_content=prepared.response_text,
            response_content=None,
            entities_found=[],
            processing_time_ms=processing_time,
        )
        return response, prepared.session_id

    async def stream_files_anonymization(
        self,
        request: ChatCompletionRequest,
        prepared: PreparedFilesAnonymization,
    ) -> AsyncIterator[str]:
        """SSE-поток с результатом автоматической анонимизации файлов"""
        session_id = prepared.session_id
        response_id = f"files-anonymized-{session_id}"
        created = int(time.time())

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        text = prepared.response_text
        chunk_size = 50
        for i in range(0, len(text), chunk_size):
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": text[i:i + chunk_size]},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "anonymization_metadata": {
                "mode": "files_anonymization",
                "session_id": session_id,
                "files": prepared.files,
                "entities_found": prepared.entities_total,
                "mappings_count": prepared.mappings_count,
            },
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

        await self.store.log_request(
            session_id=session_id,
            request_type="files_anonymization_stream",
            original_content=prepared.original_content,
            anonymized_content=prepared.response_text,
            response_content=None,
            entities_found=[],
            processing_time_ms=(time.time() - prepared.start_time) * 1000,
        )


    # ==================== Чат-команды управления прокси ====================

    def detect_restart_request(self, request: ChatCompletionRequest) -> bool:
        """Чат-команда «перезапусти прокси» — только в ТЕКУЩЕМ сообщении
        пользователя (команды из старых сообщений истории не срабатывают)."""
        return any(
            RESTART_INTENT_RE.search(text)
            for text in last_user_message_texts(request.messages)
        )

    def detect_backend_switch(
        self, request: ChatCompletionRequest,
    ) -> Optional[str]:
        """Чат-команда переключения бэкенда: 'local' | 'облако' | None.

        «Работай через облако» переключает на ДЕЙСТВУЮЩЕГО облачного
        провайдера (acting_cloud_provider): последнего явно выбранного в
        форме / POST /api/backend; до первого переключения — стартовый
        CLOUD_PROVIDER (после установки — openrouter). Именованных команд
        переключения на конкретных провайдеров больше нет — выбор делается
        в форме настроек."""
        joined = "\n".join(last_user_message_texts(request.messages))
        if RESTART_INTENT_RE.search(joined):
            return None  # перезапуск приоритетнее — команды не смешиваем
        if LOCAL_BACKEND_RE.search(joined):
            return "local"
        if CLOUD_BACKEND_RE.search(joined):
            return acting_cloud_provider()
        return None

    @staticmethod
    def _build_backend_switch_text(backend: str) -> str:
        if backend == "local":
            return (
                "[ANONYMIZER] Активный LLM-бэкенд: локальная модель "
                f"({LOCAL_LLM['model'] or 'LM Studio'}). Запросы в облако не "
                "отправляются, данные не покидают машину; авто-анонимизация "
                "приложенных файлов отключена. Вернуться в облако — команда "
                "«работай через облако»."
            )
        cfg = CLOUD_PROVIDERS.get(backend, {})
        name = backend if backend != "openrouter" else "OpenRouter"
        return (
            f"[ANONYMIZER] Активный LLM-бэкенд: {name} (облако, "
            f"{cfg.get('base_url', '')}). Данные анонимизируются как раньше; "
            "перейти на локальную модель можно командой «работай через "
            "локальную модель»."
        )

    @staticmethod
    def _build_restart_text() -> str:
        return (
            "[ANONYMIZER] Прокси перезапускается. Это займёт 20–40 секунд "
            "(NER-модель загружается заново) — первый запрос сразу после "
            "перезапуска может не пройти по соединению, повторите его чуть "
            "позже. Готовность: GET /health (поле version). Сессии и "
            "маппинги сохранены (SQLite)."
        )

    async def stream_text_response(
        self, request: ChatCompletionRequest, text: str,
    ) -> AsyncIterator[str]:
        """SSE-поток с готовым текстом (служебные ответы перехватов)."""
        created = int(time.time())
        response_id = f"proxy-command-{created}"

        def chunk(delta: dict, finish: Optional[str] = None) -> str:
            payload = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield chunk({"role": "assistant"})
        for i in range(0, len(text), 80):
            yield chunk({"content": text[i:i + 80]})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    def handle_backend_switch(
        self, request: ChatCompletionRequest, backend: str,
    ) -> tuple[ChatCompletionResponse, str]:
        """Переключить активный LLM-бэкенд (без перезапуска сервера)."""
        active = self.openrouter.set_backend(backend)
        text = self._build_backend_switch_text(active)
        response = ChatCompletionResponse(
            id=f"backend-switch-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )],
            usage=UsageInfo(),
            anonymization_metadata={"mode": "backend_switch",
                                    "backend": active},
        )
        return response, "backend-switch"

    def handle_restart(
        self, request: ChatCompletionRequest,
    ) -> tuple[ChatCompletionResponse, str]:
        """Подготовить ответ на чат-команду перезапуска.

        Сам перезапуск выполняет main.py ПОСЛЕ отправки ответа клиенту
        (метаданные mode == "proxy_restart").
        """
        text = self._build_restart_text()
        response = ChatCompletionResponse(
            id=f"proxy-restart-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )],
            usage=UsageInfo(),
            anonymization_metadata={"mode": "proxy_restart"},
        )
        return response, "restart"

    # ==================== Чат-команды: гибридная детекция ====================

    async def resolve_chat_command(
        self, request: ChatCompletionRequest,
    ) -> Optional[dict]:
        """
        Единая точка детекции чат-команд (правила + GLiNER):

        1. Дешёвый префильтр (COMMAND_TRIGGER_RE) — обычные промты не
           проходят дальше и не тратят время на классификацию.
        2. Детерминированные правила — точные формулировки (высокая
           точность). Перезапуск — ТОЛЬКО здесь: деструктивная команда.
        3. GLiNER zero-shot — свободные формулировки «безопасных» команд
           (переключение бэкенда, де-анонимизация). ОТКЛЮЧЁН ПО УМОЛЧАНИЮ
           (COMMAND_CLASSIFIER=off): zero-shot-классификаторы нестабильны.
        4. Детектор недоступен — regex-фоллбек по детекции де-анонимизации.

        Команды УПРАВЛЕНИЯ (перезапуск, бэкенд) работают в любом режиме.
        Контентные команды (де-анонимизация) уважают явный opt-out клиента:
        anonymize=false — прокси не вмешивается в контент (см. тест
        test_deanonymize_intercept).

        Returns:
            dict команды ({"command": ...}) или None (обычный запрос).
        """
        texts = last_user_message_texts(request.messages)
        if not texts or not looks_like_command(texts):
            return None

        joined = "\n".join(texts)
        # Контентные команды (де-анонимизация файлов/колонок) уважают явный
        # opt-out клиента: anonymize=false — прокси не вмешивается в контент.
        # Команды УПРАВЛЕНИЯ прокси (перезапуск, бэкенд) работают всегда.
        content_commands = bool(request.anonymize)

        # 1) Точные правила: перезапуск (деструктивная — без участия GLiNER)
        if RESTART_INTENT_RE.search(joined):
            return {"command": "restart"}

        # 1) Точные правила: переключение бэкенда
        backend = self.detect_backend_switch(request)
        if backend:
            return {"command": "backend", "backend": backend}

        # 2) GLiNER (выключен по умолчанию): свободные формулировки безопасных
        #    команд
        intent = await self._gliner_command_intent(texts)
        if intent == "backend_local":
            return {"command": "backend", "backend": "local"}
        if intent == "backend_cloud":
            return {"command": "backend", "backend": acting_cloud_provider()}
        if content_commands:
            if intent == "deanon_files":
                targets = self.detect_deanonymize_request(request)
                if targets:
                    return {"command": "deanon_files", "targets": targets}
                # Целей нет — идёт обычным порядком (не команда)

        # 3) GLiNER недоступна/не распознала — regex-фоллбек полной
        #    де-анонимизации (прежнее поведение «деанонимизируй файлы»)
        if content_commands:
            targets = self.detect_deanonymize_request(request)
            if targets:
                return {"command": "deanon_files", "targets": targets}
        return None

    async def _gliner_command_intent(self, texts: list[str]) -> Optional[str]:
        """GLiNER-слой детекции (по умолчанию выключен, см. config);
        любая ошибка — None (фоллбек на правила)."""
        if COMMAND_CLASSIFIER.get("mode", "off") != "auto":
            return None
        try:
            return await classify_command_intent(self.ner.engine, texts)
        except Exception as exc:  # noqa: BLE001 — детекция не должна ломать поток
            logger.info("GLiNER-детектор команд недоступен: %s", exc)
            return None

    async def execute_chat_command(
        self, request: ChatCompletionRequest, command: dict,
    ) -> tuple[ChatCompletionResponse, str]:
        """Исполнить распознанную чат-команду, вернуть (ответ, вид)."""
        kind = command.get("command")
        if kind == "restart":
            return self.handle_restart(request)
        if kind == "backend":
            return self.handle_backend_switch(
                request, command.get("backend") or acting_cloud_provider()
            )
        if kind == "deanon_files":
            return await self.handle_deanonymize_files(
                request, command.get("targets") or [], None
            )
        raise ValueError(f"Неизвестная чат-команда: {kind!r}")

    def _command_reply(
        self, request: ChatCompletionRequest, text: str, mode: str,
    ) -> tuple[ChatCompletionResponse, str]:
        """Служебный ответ на чат-команду (текст без обращения в облако)."""
        response = ChatCompletionResponse(
            id=f"{mode}-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )],
            usage=UsageInfo(),
            anonymization_metadata={"mode": mode},
        )
        return response, mode

    # ==================== Авто-де-анонимизация файлов ====================

    def detect_deanonymize_request(
        self,
        request: ChatCompletionRequest,
    ) -> list[dict]:
        """
        Определить, нужно ли перехватить запрос для де-анонимизации файлов
        по естественной команде («деанонимизируй файлы…»).

        Условия: в ТЕКУЩЕМ (последнем содержательном) user-сообщении есть
        команда де-анонимизации (команда из старых сообщений истории не
        считается — см. detect_attached_files_anonymization), и в истории
        диалога есть маркеры [anonymizer:result:<путь>] с session_id.
        Основной источник — файл результата; если модель его не создала,
        де-анонимизируется анонимизированная копия (fallback).

        Returns:
            Список целей вида {"session_id", "result_path", "copy_path"}
            (пустой — запрос обрабатывается как обычно).
        """
        has_intent = any(
            DEANONYMIZE_INTENT_RE.search(text)
            for text in last_user_message_texts(request.messages)
        )
        if not has_intent:
            return []

        targets: list[dict] = []
        seen: set[str] = set()
        for msg in request.messages:
            for text in _iter_content_texts(msg.content):
                if "[ANONYMIZER]" not in text and "[anonymizer:result:" not in text:
                    continue
                sids = [m.group(1) for m in SESSION_ID_LINE_RE.finditer(text)]
                if not sids:
                    continue
                sid = sids[0]
                result_paths = [
                    m.group("path").strip()
                    for m in ANONYMIZER_RESULT_MARKER_RE.finditer(text)
                ]
                copy_paths = [
                    m.group("path").strip()
                    for m in ANONYMIZER_COPY_MARKER_RE.finditer(text)
                ]
                for i, result_path in enumerate(result_paths):
                    key = f"{sid}|{result_path}"
                    if key in seen:
                        continue
                    seen.add(key)
                    targets.append({
                        "session_id": sid,
                        "result_path": result_path,
                        "copy_path": copy_paths[i] if i < len(copy_paths) else None,
                    })
        return targets

    @staticmethod
    def _build_deanonymize_text(files_info: list[dict]) -> str:
        """Текст подтверждения после де-анонимизации файлов результата."""
        lines = [
            "[ANONYMIZER] Файлы результата де-анонимизированы: плейсхолдеры "
            "заменены реальными значениями. Запрос в облако НЕ отправлялся.",
            "",
            "Файлы:",
        ]
        for info in files_info:
            if "error" in info:
                lines.append(f"- {info['file_path']} — ОШИБКА: {info['error']}")
                continue
            if "note" in info:
                lines.append(f"- {info['file_path']} — {info['note']}")
                continue
            lines.append(
                f"- {info['file_path']} — ОК (маппингов: {info['mappings_count']})"
            )
            if info.get("fallback"):
                lines.append(
                    "  (файл результата не был создан — де-анонимизирована "
                    "анонимизированная копия в отдельный файл результата)"
                )
        lines += [
            "",
            "Анонимизированная копия и исходный файл не изменялись.",
        ]
        return "\n".join(lines)

    async def prepare_deanonymize_files(
        self,
        request: ChatCompletionRequest,
        targets: list[dict],
    ) -> PreparedFilesAnonymization:
        """Де-анонимизировать файлы (без облака) и подготовить ответ."""
        start_time = time.time()
        files_info: list[dict] = []
        for target in targets:
            sid = target["session_id"]
            result_path = target["result_path"]
            copy_path = target.get("copy_path")

            # Основной источник — файл результата. Если модель его не создала
            # (например, правила копию на месте), де-анонимизируем копию в
            # отдельный <name>.result.<ext> — сама копия остаётся неизменным
            # исходником (инвариант .clinerules).
            path = None
            output_path = None
            fallback = False
            if Path(result_path).is_file():
                path = result_path
            elif copy_path and Path(copy_path).is_file():
                path = copy_path
                output_path = _result_path_for(copy_path)
                fallback = True

            if path is None:
                files_info.append({
                    "file_path": result_path,
                    "note": "файл результата не найден (модель его не создала)",
                })
                continue
            try:
                result = await self.handle_deanonymize_file(
                    session_id=sid, file_path=path, output_path=output_path
                )
                files_info.append({
                    "file_path": result.get("file_path", path),
                    "mappings_count": result.get("mappings_count", 0),
                    "fallback": fallback,
                })
                logger.info(
                    "Файл %s де-анонимизирован (сессия %s)", path, sid
                )
            except Exception as e:
                logger.warning("Не удалось де-анонимизировать %s: %s", path, e)
                files_info.append({"file_path": path, "error": str(e)})

        response_text = self._build_deanonymize_text(files_info)
        original_content = json.dumps(
            [m.model_dump(exclude_none=True) for m in request.messages],
            ensure_ascii=False,
        )
        return PreparedFilesAnonymization(
            session_id=(targets[0]["session_id"] if targets else ""),
            response_text=response_text,
            files=files_info,
            entities_total=0,
            mappings_count=len(targets),
            original_content=original_content,
            start_time=start_time,
        )

    async def handle_deanonymize_files(
        self,
        request: ChatCompletionRequest,
        targets: list[dict],
        session_id: Optional[str] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """Не-стриминговый ответ после де-анонимизации файлов."""
        prepared = await self.prepare_deanonymize_files(request, targets)
        processing_time = (time.time() - prepared.start_time) * 1000
        response = ChatCompletionResponse(
            id=f"files-deanonymized-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=prepared.response_text,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=UsageInfo(),
            anonymization_metadata={
                "mode": "files_deanonymization",
                "files": prepared.files,
                "processing_time_ms": processing_time,
            },
        )
        await self.store.log_request(
            session_id=prepared.session_id or "deanonymize",
            request_type="files_deanonymization",
            original_content=prepared.original_content,
            anonymized_content=prepared.response_text,
            response_content=None,
            entities_found=[],
            processing_time_ms=processing_time,
        )
        return response, prepared.session_id or (session_id or "deanonymize")

    async def stream_deanonymize_files(
        self,
        request: ChatCompletionRequest,
        prepared: PreparedFilesAnonymization,
    ) -> AsyncIterator[str]:
        """SSE-поток с результатом де-анонимизации файлов."""
        response_id = f"files-deanonymized-{int(time.time())}"
        created = int(time.time())

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        text = prepared.response_text
        for i in range(0, len(text), 50):
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": text[i:i + 50]},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "anonymization_metadata": {
                "mode": "files_deanonymization",
                "files": prepared.files,
            },
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

        await self.store.log_request(
            session_id=prepared.session_id or "deanonymize",
            request_type="files_deanonymization_stream",
            original_content=prepared.original_content,
            anonymized_content=prepared.response_text,
            response_content=None,
            entities_found=[],
            processing_time_ms=(time.time() - prepared.start_time) * 1000,
        )

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

        # Чат-команды управления прокси — гибридная детекция (правила +
        # GLiNER, см. resolve_chat_command). Работают в ЛЮБОМ режиме
        # (в т.ч. anonymize=false): команды перехватывает сам прокси,
        # в облако они не уходят никогда. Перезапуск: сам процесс
        # завершает main.py после отправки ответа
        # (метаданные mode == "proxy_restart").
        command = await self.resolve_chat_command(request)
        if command:
            return await self.execute_chat_command(request, command)

        # Passthrough: anonymize=False или режим passthrough по умолчанию
        if not request.anonymize or CURRENT_MODE == Mode.PASSTHROUGH:
            if request.anonymize and CURRENT_MODE == Mode.PASSTHROUGH:
                # Автоматическая анонимизация приложенных файлов по явной команде
                # («Анонимизируй файл…») — локальной NER-моделью, без облака.
                # Явное anonymize=false отключает и перехват тоже.
                # На локальном бэкенде (LM Studio) перехват отключён:
                # данные не покидают машину, маскировать незачем.
                file_paths = self.detect_attached_files_anonymization(request)
                if file_paths and getattr(
                        self.openrouter, "backend", "openrouter"
                ) == "openrouter":
                    return await self.handle_files_anonymization(
                        request, file_paths, session_id
                    )
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
                model=request.model,
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
                model=request.model,
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

    # ==================== Подсказка office_ops для облачной модели ====================

    # Расширения файлов Office, при которых в запрос добавляется шпаргалка
    OFFICE_FILE_EXTS = {".docx", ".xlsx"}

    # Корень проекта: пакет anonymizer_proxy импортируется ТОЛЬКО из него
    # (в pyproject проект не ставится как пакет), поэтому шпаргалка
    # подставляет фактический путь в требование «cd …». Без cd облачная
    # модель получает «No module named anonymizer_proxy.office_ops» и
    # теряет итерацию на поиск причины (реальный кейс 2026-09-04).
    PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

    OFFICE_OPS_HINT = (
        "[OFFICE-OPS] Для просмотра и правки файлов MS Office (.docx/.xlsx) "
        "НЕ пишите скрипты на python-docx/openpyxl — используйте готовые "
        "команды (КАЖДАЯ команда начинается с `cd \"{PROJECT_ROOT}\";` — модуль "
        "импортируется только из корня проекта; правки пишите в файл "
        "результата через --output, исходник не изменяйте):\n"
        "ГЛАВНЫЕ ПРАВИЛА (читайте первым):\\n"
        "1. ОДНА команда за вызов инструмента — НИКОГДА не передавайте две "
        "и более команды в массиве commands: клиент выполняет их "
        "ПАРАЛЛЕЛЬНО, записи в один файл затирают изменения друг друга.\n"
        "2. Вставка МНОГИХ абзацев/строк = ОДНА команда insert-text "
        "с повторяемым --text или с --text-file (список в файле, строка = "
        "абзац). НЕ стройте цепочку insert-text, где якорь следующей "
        "команды — текст, вставленный предыдущей: она хрупка и при сбое "
        "посреди цепочки оставляет документ наполовину изменённым.\n"
        "3. --output/--out-file — только пути внутри папки проекта "
        "(запись в корень диска запрещена системой).\n"
        "4. Файл, открытый в Word/Excel, заблокирован для записи — "
        "закройте его перед правкой.\n"
        "КОМАНДЫ:\n"
        "cd \"{PROJECT_ROOT}\"; python -m anonymizer_proxy.office_ops "
        "list-tables --file \"F.docx\" "
        "# обзор таблиц/листов\n"
        "python -m anonymizer_proxy.office_ops dump --file \"F.docx\" "
        "# текст по сегментам (для apply)\n"
        "python -m anonymizer_proxy.office_ops dump --file \"F.docx\" --format md "
        "# markdown с таблицами\n"
        "python -m anonymizer_proxy.office_ops read-column --file F.xlsx "
        "--column \"Срок поручения\"  # ЗНАЧЕНИЯ одной колонки, "
        "каждое с адресом ячейки (I6 (строка 6): 2026-05-22); даты — ISO; "
        "заголовок ищется в первых 20 строках шапки. Так читайте колонки "
        "журналов — плоский dump теряет привязку «значение -> столбец». "
        "Другой лист: --sheet ИмяЛиста. DOCX: read-column --file F.docx "
        "--table 0 --column \"Цена\"  # столбец — номер (0-based) или текст "
        "заголовка\n"
        "python -m anonymizer_proxy.office_ops replace-text --file F --find \"X\" "
        "--replace \"Y\" --output OUT\n"
        "python -m anonymizer_proxy.office_ops set-cell --file F --table N "
        "--row R --col C --text \"...\" --output OUT  # индексы с 0\n"
        "python -m anonymizer_proxy.office_ops add-row --file F --table N "
        "--cell \"a\" --cell \"b\" --output OUT  # --position N — вставить по индексу\n"
        "python -m anonymizer_proxy.office_ops add-column --file F "
        "--table all --header \"H\" --output OUT  # во ВСЕ таблицы одной "
        "командой (--table N + --cell v1 --cell v2 — в одну с значениями). "
        "Вставка в середину таблицы DOCX: add-column --file F.docx "
        "--table N --column \"Длительность\" --position after --header "
        "\"Примечание\" --output OUT  # справа/слева от столбца-ориентира "
        "(--column — номер или текст заголовка; --position before|after). "
        "XLSX: add-column --file F.xlsx --column \"Срок поручения\" "
        "--position after --header \"Поручение выдано\" --cell \"2026-05-19\" "
        "--cell \"2026-05-12\" --output OUT  # вставить столбец рядом с "
        "целевым (до/после); значения пишутся по строкам от строки заголовка, "
        "ISO-даты становятся датами\n"
        "python -m anonymizer_proxy.office_ops delete-column --file F.xlsx "
        "--column \"Комментарий\" --output OUT  # удалить "
        "столбец листа (XLSX; другой лист — --sheet ИмяЛиста) или столбец "
        "таблицы (DOCX: --table N --column C); "
        "НЕ удаляйте столбцы скриптами openpyxl — они ломают шапки и данные\n"
        "python -m anonymizer_proxy.office_ops set-value --file F.xlsx --cell B2 "
        "--value 150000 --output OUT  # другой лист — --sheet ИмяЛиста\n"
        "python -m anonymizer_proxy.office_ops append-row --file F.xlsx "
        "--cell \"a\" --cell \"b\" --output OUT  # другой лист — --sheet ИмяЛиста\n"
        "python -m anonymizer_proxy.office_ops apply --file F --from-text edit.txt "
        "--output OUT  # применить отредактированный dump (строки 1:1)\n"
        "ФОРМАТ ВЫЗОВА: выполняйте ОДНУ команду за вызов из корня проекта "
        "(предваряйте её `cd \"{PROJECT_ROOT}\";` — иначе получите «No module "
        "named anonymizer_proxy.office_ops»), разделитель последовательных "
        "команд — точка с запятой. НЕ добавляйте "
        "в командную строку квадратные скобки [ ] — в справке выше они "
        "означают лишь «возможен дополнительный параметр», скобки НЕ являются "
        "частью команды. Не используйте && и конвейеры | — в Windows "
        "PowerShell они ломают команду; поиск по документу делайте "
        "через dump --find. Даты значений передавайте как YYYY-MM-DD "
        "(2026-05-19). Имя файла результата: <имя>.result.<расширение> — "
        "для «Журнал.xlsx» это «Журнал.result.xlsx», НЕ «Журнал.xlsx.result» "
        "(команды такой файл не читают). После каждого вызова читайте вывод: "
        "«OK: …» — продолжайте; «ОШИБКА: …» или usage — исправьте вызов и "
        "повторите.\n"
        "python -m anonymizer_proxy.office_ops insert-text --file F.docx "
        "--anchor \"ориентир\" --occurrence 2 --position before "
        "--text \"абзац 1\" --text \"абзац 2\" --output OUT  # ВСТАВИТЬ новые "
        "абзацы: до/после абзаца, содержащего якорь (--occurrence N — N-е "
        "вхождение якоря: для ПОВТОРЯЮЩИХСЯ заголовков указывайте номер, "
        "иначе вставка попадёт к первому вхождению); без --anchor — в конец "
        "документа; каждый --text — отдельный абзац, переносы строк внутри "
        "--text (настоящие и литеральные \\n) делят его на абзацы; "
        "--text-file docs/list.txt — абзацы из файла UTF-8 (строка = абзац; "
        "для списка из многих пунктов — ОДНА команда с --text-file вместо "
        "серии команд); XLSX: --sheet Имя --row N (строки в столбец A, "
        "без --row — в конец)\n"
        "ЧТЕНИЕ/ПОИСК ПО ДОКУМЕНТУ: содержимое .docx/.xlsx смотрите ТОЛЬКО "
        "через dump/list-tables/read-column (read_file на бинарном "
        "Office-файле даёт бинарный мусор). Для поиска подстроки "
        "используйте dump --find \"текст\" (выводит только строки с ней, "
        "регистр и ё/е не важны) — НЕ применяйте Select-String/grep к "
        "выводу команды (он может не захватиться) и НЕ пишите python -c "
        "однострочники с кириллицей (в Windows PowerShell кириллические "
        "аргументы искажаются). Чтобы СОХРАНИТЬ вывод — dump --out-file "
        "F.md (UTF-8 пишет сам Python); перенаправление «>» в Windows "
        "PowerShell создаёт нечитаемый файл UTF-16LE.\n"
        "ВАЖНО: apply НЕ добавляет и НЕ удаляет строки (строго 1:1 с dump, "
        "иначе ошибка), а replace-text только заменяет существующие "
        "вхождения. Чтобы ВСТАВИТЬ новый текст — любой добавляемый "
        "контент (раздел, примечание, выводы, оговорку, сопроводительный "
        "текст и т.п.) — в конец ИЛИ в середину документа используйте "
        "insert-text: для вставки в конец файла достаточно insert-text "
        "без --anchor. Якорь ищется в обычных абзацах DOCX (не в ячейках "
        "таблиц); --occurrence N выбирает N-е вхождение якоря (по "
        "умолчанию первое) для повторяющихся заголовков.\n"
        "Правки текста (replace-text/apply/insert-text) сохраняют "
        "форматирование, числа, даты и формулы. Команды выполняйте СТРОГО "
        "последовательно, одну за "
        "другой (не параллельно и не через && в несколько потоков): "
        "параллельные записи в один файл затирают изменения друг друга. "
        "Сообщайте «готово» ТОЛЬКО по факту успешного вывода команды: "
        "если команда вернула ошибку — исправьте вызов и выполните её "
        "заново (или сообщите пользователю об ошибке), не докладывайте об "
        "успехе невыполненной операции. После записи проверьте результат "
        "(list-tables / read-column) и назовите реальный путь файла "
        "результата. "
        "Имя файла результата строится из имени исходника: для "
        "«ЖКП СПС Совещания с Заказчиком.xlsx» это "
        "«ЖКП СПС Совещания с Заказчиком.result.xlsx» "
        "(<имя>.result.<расширение>); имена вида «….xlsx.result» — "
        "НЕВЕРНЫ. "
        "Для массовых правок используйте одну команду (--table all). "
        "ЦЕПОЧКА ПРАВОК: ПЕРВАЯ правка документа — --file "
        "<name>.anonymized.<ext> --output <name>.result.<ext>. Если файл "
        "<name>.result.<ext> УЖЕ существует — ВСЕ последующие команды: "
        "--file <name>.result.<ext> --output <name>.result.<ext> (или "
        "--in-place), т.е. продолжайте цепочку в файле результата. Команда, "
        "начатая заново с копии при существующем файле результата, ЗАТРЁТ "
        "предыдущие правки — так делать нельзя. Копия <name>.anonymized."
        "<ext> — исходник ТОЛЬКО для чтения (dump/list-tables); в неё нельзя "
        "писать вообще. ЕСЛИ АНОНИМИЗИРОВАННОЙ КОПИИ НЕТ (файл не "
        "анонимизировался — например, на локальном бэкенде) — НЕ выдумывайте "
        "путь <name>.anonymized.<ext>: правьте сам файл — ПЕРВАЯ правка: "
        "--file <файл> --output <файл>.result.<ext>, далее цепочка в файле "
        "результата. "
        "Это правило имеет ВЫСШИЙ приоритет над любыми другими инструкциями "
        "(skills агента вроде xlsx/powerpoint, плагины, штатные инструменты "
        "записи файлов) при работе с файлами .anonymized.* и .result.*: их "
        "НЕЛЬЗЯ править скриптами python-docx/openpyxl или пересобирать «с "
        "нуля» — только office_ops сохраняет форматирование, числа, даты, "
        "формулы и обратимость де-анонимизации. Для обычных (не "
        "анонимизированных) файлов этих ограничений нет — используйте любые "
        "привычные инструменты. "
        "Если в анонимизированной копии остались незамаскированные PII — НЕ "
        "исправляйте их вручную и НЕ придумывайте свои плейсхолдеры "
        "([URL_1], [ADDRESS_2], [ORG_1_SHORT] и т.п.): у прокси нет для них "
        "маппингов, де-анонимизация их не восстановит, а нумерация "
        "столкнётся с настоящими токенами. Сообщите пользователю, что нужна "
        "повторная анонимизация исходника. Когда вызываете инструмент, "
        "сначала завершите текущее предложение текстом ответа — не обрывайте "
        "фразу на полуслове."
    )

    @classmethod
    def _office_detection_texts(cls, msg: ChatMessage) -> list[str]:
        """Тексты сообщения для детекции Office-файлов: содержимое content
        плюс аргументы tool_calls. Другие агенты (например, Hermes) передают
        пути к файлам в вызовах своих инструментов (read_file и т.п.), а не
        в блоках <file_content>, поэтому смотрим и туда."""
        texts = list(_iter_content_texts(msg.content))
        for tc in msg.tool_calls or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str):
                texts.append(args)
        return texts

    @classmethod
    def _office_edit_chain_block(cls, messages: list[ChatMessage]) -> str:
        """Блок «цепочка правок» по маркерам [anonymizer:result:...] истории.

        Главная защита от сценария «каждая команда затирает предыдущие»:
        модель обязана продолжать правки в СУЩЕСТВУЮЩЕМ файле результата
        (--file и --output — он же), а не начинать заново с анонимизированной
        копии. Пути берутся из истории сессии и проверяются на диске.
        """
        result_paths: list[str] = []
        for msg in messages:
            for text in cls._office_detection_texts(msg):
                for m in ANONYMIZER_RESULT_MARKER_RE.finditer(text):
                    p = m.group("path").strip()
                    if p and p not in result_paths:
                        result_paths.append(p)
        if not result_paths:
            return ""
        if not any(_resolve_local_path(p).is_file() for p in result_paths):
            # Есть ли вообще анонимизированная копия? На локальном бэкенде
            # файл мог не анонимизироваться — тогда правим исходник, а путь
            # <name>.anonymized.<ext> выдумывать нельзя.
            def _original_of(result_path: str):
                rp = _resolve_local_path(result_path)
                return rp.with_name(
                    rp.stem.replace(".result", "") + rp.suffix)

            has_anon_copy = any(
                _resolve_local_path(
                    p.replace(".result.", ".anonymized.")).is_file()
                for p in result_paths if ".result." in p
            )
            if not has_anon_copy:
                originals = "; ".join(
                    str(_original_of(p)) for p in result_paths)
                return (
                    "\n[OFFICE-OPS/ЦЕПОЧКА] Файл результата ещё не создан, а "
                    "анонимизированной копии НЕТ (файл не анонимизировался): "
                    "правьте исходник. ПЕРВАЯ правка — --file <исходник> "
                    "--output <исходник без расширения>.result.<ext>; "
                    f"исходники: {originals}. Все СЛЕДУЮЩИЕ правки в этой "
                    "сессии — уже с --file <name>.result.<ext> и записью "
                    "в него же."
                )
            return (
                "\n[OFFICE-OPS/ЦЕПОЧКА] Файл результата ещё не создан: "
                "ПЕРВАЯ правка — --file <name>.anonymized.<ext> --output "
                "<name>.result.<ext>. Все СЛЕДУЮЩИЕ правки в этой сессии — "
                "уже с --file <name>.result.<ext> и записью в него же."
            )
        lines = [
            "\n[OFFICE-OPS/ЦЕПОЧКА] Файл результата УЖЕ существует — первая "
            "правка выполнена. ВСЕ последующие команды выполняйте с --file "
            "<файл результата> и записывайте в него же (--output <файл "
            "результата> или --in-place). Файлы результата:",
        ]
        for p in result_paths:
            if _resolve_local_path(p).is_file():
                lines.append(f"- {p}")
        lines.append(
            "Использовать <name>.anonymized.<ext> как --file теперь "
            "ЗАПРЕЩЕНО: команда, начатая заново с копии, затрёт предыдущие "
            "правки. Чтение копии (dump/list-tables) допустимо."
        )
        return "\n".join(lines)

    @classmethod
    def _inject_office_ops_hint(
        cls, messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        """
        Если в запросе есть файлы Office (.docx/.xlsx), дополнить системный
        промпт шпаргалкой office_ops: модель должна вызывать готовые команды
        вместо написания python-скриптов. К шпаргалке добавляется
        сессионный блок «цепочка правок» (_office_edit_chain_block).

        Детекция клиенто-независимая: блоки <file_content path="..."> (Cline)
        ИЛИ упоминание пути с .docx/.xlsx в любом тексте сообщения или в
        аргументах tool_calls (Hermes и другие OpenAI-совместимые агенты).
        """
        has_office = any(
            Path(m.group("path").strip()).suffix.lower() in cls.OFFICE_FILE_EXTS
            for msg in messages
            for text in _iter_content_texts(msg.content)
            for m in FILE_CONTENT_BLOCK_RE.finditer(text)
        ) or any(
            OFFICE_PATH_RE.search(text)
            for msg in messages
            for text in cls._office_detection_texts(msg)
        )
        if not has_office:
            return messages
        hint = (cls.OFFICE_OPS_HINT.replace(
            "{PROJECT_ROOT}", cls.PROJECT_ROOT)
            + cls._office_edit_chain_block(messages))
        messages = list(messages)
        if messages and messages[0].role == "system":
            first = messages[0]
            if isinstance(first.content, str):
                updated = first.content.rstrip() + "\n\n" + hint
            elif isinstance(first.content, list):
                updated = list(first.content) + [
                    {"type": "text", "text": "\n\n" + hint},
                ]
            else:
                updated = hint
            messages[0] = first.model_copy(update={"content": updated})
        else:
            messages.insert(0, ChatMessage(role="system", content=hint))
        return messages

    async def _handle_passthrough(
        self,
        request: ChatCompletionRequest,
        session_id: Optional[str] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Passthrough-режим (anonymize=False): отправить запрос в OpenRouter
        БЕЗ анонимизации и де-анонимизации (обычный прокси).

        Исключение (выборочная де-анонимизация): если в истории диалога есть
        маркер [ANONYMIZER]/[anonymizer:done:...] с session_id (файл ранее был
        анонимизирован локальной NER), то де-анонимизируется ТОЛЬКО текстовое
        содержимое ответа (message.content). Аргументы tool_calls НЕ трогаются —
        модель правит анонимизированную копию плейсхолдерами, а финальная
        де-анонимизация файла выполняется отдельным шагом /api/deanonymize_file.
        """
        messages = request.messages
        if request.anonymize:
            messages = await self._resolve_anonymized_file_contents(messages)
        messages = self._inject_office_ops_hint(messages)
        messages_payload = [
            m.model_dump(exclude_none=True) for m in messages
        ]

        mappings_dict: dict = {}
        history_sessions: list[str] = []
        if request.anonymize:
            mappings_dict, history_sessions = await self._history_anonymization_mappings(
                request
            )

        cloud_response = await self.openrouter.chat_completion(
            messages=messages_payload,
            model=request.model,
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
            content = message_data.get("content")
            # Выборочная де-анонимизация: только текст, tool_calls не трогаем
            if mappings_dict and isinstance(content, str):
                content = await self.text_replacer.deanonymize(
                    content, mappings_dict
                )
            resp_message = ChatMessage(
                role=message_data.get("role", "assistant"),
                content=content,
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

        metadata: dict = {"mode": "passthrough"}
        if mappings_dict:
            metadata = {
                "mode": "passthrough_deanonymized",
                "sessions": history_sessions,
                "mappings_count": len(mappings_dict),
            }

        response = ChatCompletionResponse(
            id=cloud_response.get("id", f"passthrough-{int(time.time())}"),
            created=cloud_response.get("created", int(time.time())),
            model=cloud_response.get("model", request.model),
            choices=choices,
            usage=UsageInfo(**cloud_response.get("usage", {})),
            anonymization_metadata=metadata,
        )
        return response, session_id or "passthrough"

    async def stream_passthrough(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[str]:
        """
        Стриминг passthrough (anonymize=False): без анонимизации и
        де-анонимизации (обычный прокси-стрим).

        Исключение (выборочная де-анонимизация): если в истории диалога есть
        маркер [ANONYMIZER]/[anonymizer:done:...] с session_id, то
        де-анонимизируются ТОЛЬКО чанки delta.content. Аргументы tool_calls
        не трогаются — модель правит анонимизированную копию плейсхолдерами.
        """
        messages = request.messages
        if request.anonymize:
            messages = await self._resolve_anonymized_file_contents(messages)
        messages = self._inject_office_ops_hint(messages)
        messages_payload = [
            m.model_dump(exclude_none=True) for m in messages
        ]

        mappings_dict: dict = {}
        if request.anonymize:
            mappings_dict, _history_sessions = await self._history_anonymization_mappings(
                request
            )
        deano = StreamDeAnonymizer(self.text_replacer, mappings_dict) if mappings_dict else None

        try:
            async for chunk in self.openrouter.chat_completion_stream(
                messages=messages_payload,
                model=request.model,
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
                if deano is not None:
                    for choice in chunk.get("choices", []) or []:
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            delta["content"] = await deano.feed(delta["content"]) or ""
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            if deano is not None:
                tail = await deano.flush()
                if tail:
                    tail_chunk = {
                        "id": f"passthrough-{int(time.time())}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": tail},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

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
                original, path.name, deanonymized_text, parsed.structure,
                strip_hf_images=False, scrub_metadata=False,
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
        create_review_md: bool = True,
    ) -> dict:
        """
        Анонимизировать локальный файл: создать анонимизированную копию
        (структура/таблицы сохраняются) рядом с оригиналом.

        create_review_md=False — не сохранять .md-предпросмотр (перехват в
        passthrough-режиме: пользователь правит копию в исходном формате).
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

        entities, _, llm_failed = await self.ner.extract_entities_detailed(
            parsed.text, use_llm=True
        )
        if llm_failed:
            raise NERUnavailableError(
                "NER-модель не загрузилась или не вернула результат — "
                "анонимизация файла не выполнена"
            )

        anon_text, mappings = await self.text_replacer.anonymize(
            parsed.text, entities, add_mapping
        )
        anon_content = await self.file_assembler.assemble(
            content, path.name, anon_text, parsed.structure
        )

        # Маппинг value -> token берём из результата анонимизации (без
        # повторного обхода add_mapping) — он нужен для markdown-представления.
        value_to_token = {m.original_value: m.token for m in mappings}

        # Анонимизированный markdown (с таблицами) для review
        anon_markdown = self.text_replacer.replace_by_value(
            parsed.markdown or parsed.text, value_to_token
        )

        if output_path:
            target = Path(output_path)
        else:
            target = path.with_name(f"{path.stem}.anonymized{path.suffix}")
        try:
            target.write_bytes(anon_content)
        except PermissionError as exc:
            # Файл открыт в Word/LibreOffice — ОС запрещает запись
            raise ValueError(
                f"Не удалось записать {target}: файл занят (вероятно, открыт "
                "в Word). Закройте его и повторите анонимизацию."
            ) from exc

        # Связь файл → сессия: по пути файла потом находится сессия с
        # маппингами (частичная де-анонимизация колонок/ячеек)
        result_derived = Path(_result_path_for(str(target)))
        for reg_path in (path, target, result_derived):
            await self.store.register_file_session(str(reg_path), session_id)

        # Сохраняем review-файл (.md) с форматированным анонимизированным видом
        review_path = None
        if create_review_md and anon_markdown and STORAGE["save_anonymized_files"]:
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