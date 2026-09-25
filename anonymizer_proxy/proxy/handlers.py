"""
Обработчики запросов для прокси-сервера
Содержит основную логику анонимизации/де-анонимизации
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator, Optional

from ..anonymizer.ner_service import NERService, NERUnavailableError
from ..anonymizer.file_parser import (
    FileParser,
    FileAssembler,
    ANON_MARKER,
    read_anon_marker,
)
from ..anonymizer.mapping_store import MappingStore
from ..anonymizer.replacer import (
    TextReplacer,
    MessageAnonymizer,
    StreamDeAnonymizer,
    split_entities_by_segments,
)
from ..config import (
    Mode, CURRENT_MODE, STORAGE, LOCAL_LLM,
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
from .command_args import is_name_like, parse_name_list, parse_placeholder_spec
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
    REVEAL_INTENT_RE,
    RESTART_INTENT_RE,
    EXTRA_ANONYMIZE_INTENT_RE,
    PLACEHOLDER_DEANON_INTENT_RE,
    PLACEHOLDER_TOKEN_RE,
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


def _help_command(text: str) -> dict:
    """Anti-fallthrough: команда распознана, а аргументы — нет.

    Возвращает служебную команду-подсказку вместо провала в обобщённую
    семантику (полная деанонимизация / whole-file NER): восстанавливать или
    маскировать больше, чем попросил пользователь, нельзя.
    """
    return {"command": "cmd_help", "text": text}


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Атомарная перезапись файла (временный файл + os.replace)."""
    tmp = path.with_name(path.name + ".anonymizer-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except PermissionError as exc:
        # Файл занят: открыт в Word/LibreOffice — ОС запрещает замену.
        # Текст осмысленный (не сырой PermissionError): он попадает в
        # отчёт пользователю (багрепорт 2026-09-10).
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(
            f"не удалось записать файл: занят (вероятно, открыт в "
            f"Word/LibreOffice) — {path.name}. Закройте его в редакторе "
            "и повторите команду") from exc
    except Exception:
        # Не оставлять осиротевший tmp-файл после любого сбоя записи
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


# Число-подобное значение из команды «скрой эти данные»: цифры, разрядные
# пробелы, точка/запятая (десятичный разделитель), знак
_NUMERIC_VALUE_RE = re.compile(r"[+-]?\d[\d \u00A0.,]*")


class PreparedFilesAnonymization:
    """
    Результат автоматической анонимизации приложенных файлов
    (manual-режим): файлы уже обработаны локальной NER-моделью,
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
        # содержимое файла data/anonymized_files/... идентично canonical_result.
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
    в файл и является проверяемой проекцией контекста, реально
    отправляемого в облако.
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

    # Сколько секунд хранить завершённую запись коалесценции анонимизации
    # файла: поздние дубликаты запросов получают готовый результат мгновенно
    _INFLIGHT_TTL_SECONDS = 600


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
        # Коалесценция параллельных анонимизаций одного файла (основной
        # запрос + вспомогательные Hermes — title_generation и т.п. — с той
        # же историей диалога): norm-путь -> (asyncio.Task, время создания).
        # Багрепорт 2026-09-08: 4 полных NER-прогона одного файла.
        self._file_anon_inflight: dict = {}

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

        Обычные (не анонимизированные) файлы не трогаем — контракт manual:
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
        приложенных файлов (только manual-режим).

        Условия перехвата:
        1. В ТЕКУЩЕМ (последнем содержательном) user-сообщении есть команда
           анонимизации («скрой данные/всё…» / «anonymize…»). Команда из старых
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
            # (AGENTS.md и т.п.): реальный кейс 2026-09-05 — «скрой
            # все данные» анонимизировало AGENTS.md из корня проекта
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
                # («скрой все данные c:\docs\KP_IRIS.docx»)
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
            # Временные lock-файлы Office (~$Имя.docx/.xlsx — «owner files»
            # Word/Excel, ~160 байт с сигнатурой \x05Sas) существуют на диске
            # и проходят по маске расширения, но документами не являются:
            # их парсинг даёт BadZipFile и раньше ронял весь перехват
            # (багрепорт 2026-09-08: «Provider error: File is not a zip
            # file» при «скрой все данные» в Hermes, когда рядом с целевым
            # документом в истории оказался lock-файл открытого в Word
            # документа docs\~$_IRIS.result.docx).
            if resolved.name.startswith("~$"):
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

    @staticmethod
    def _reusable_copy(path: Path) -> Optional[Path]:
        """
        Путь к существующей анонимизированной копии, если она «свежее»
        исходника (исходник не менялся с момента её создания). None —
        переиспользовать нельзя: копии нет либо исходник был изменён
        (в этом случае нужна повторная анонимизация).

        Допуск 1 секунда — на округление времени в файловых системах.
        """
        if path.stem.lower().endswith(".anonymized"):
            return None
        target = path.with_name(f"{path.stem}.anonymized{path.suffix}")
        try:
            if (target.stat().st_mtime + 1.0) >= path.stat().st_mtime:
                return target
        except OSError:
            pass
        return None

    async def _anonymize_file_dedup(
        self,
        file_path: str,
        session_id: Optional[str],
        allow_reuse: bool = True,
    ) -> dict:
        """
        Анонимизация файла с коалесценцией параллельных дубликатов.

        Hermes параллельно с основным запросом шлёт вспомогательные
        (title_generation и т.п.) с той же историей диалога: без коалесценции
        каждый такой запрос запускал полный NER-прогон того же файла заново
        (багрепорт 2026-09-08: 4 анонимизации одного файла). Параллельные и
        пришедшие чуть позже запросы дожидаются уже запущенной задачи и
        переиспользуют её результат (и её сессию маппингов).

        Завершённые записи хранятся _INFLIGHT_TTL_SECONDS: поздний дубликат
        получает готовый результат мгновенно, без нового прогона.
        """
        key = f"{norm_fs_path(file_path)}|{bool(allow_reuse)}"
        now = time.monotonic()
        for stale in [
            k for k, (_, ts) in self._file_anon_inflight.items()
            if now - ts > self._INFLIGHT_TTL_SECONDS
        ]:
            self._file_anon_inflight.pop(stale, None)

        entry = self._file_anon_inflight.get(key)
        if entry is None:
            task = asyncio.create_task(self.handle_anonymize_file(
                file_path,
                session_id=session_id,
                create_review_md=False,
                allow_reuse=allow_reuse,
            ))
            self._file_anon_inflight[key] = (task, now)
        else:
            task = entry[0]
        # shield: если запрос-дубликат отвалился (таймаут клиента), общая
        # задача анонимизации должна доработать для остальных
        return await asyncio.shield(task)

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

        # Смешанный набор (часть файлов уже анонимизирована, часть — нет):
        # переиспользование даст корректные маппинги только для части файлов
        # (у каждой копии свои токены своей сессии), поэтому в этом случае
        # обрабатываем всё заново — консистентность важнее скорости.
        reusable = [
            self._reusable_copy(Path(fp)) is not None for fp in file_paths
        ]
        allow_reuse = not (any(reusable) and not all(reusable))

        files_info: list[dict] = []
        entities_total = 0
        result_sids: list[str] = []
        for file_path in file_paths:
            try:
                result = await self._anonymize_file_dedup(
                    file_path, session_id, allow_reuse
                )
                result_sid = result.get("session_id") or session_id
                result_sids.append(result_sid)
                info = {
                    "original_file": result["original_file"],
                    "anonymized_file": result["anonymized_file"],
                    "entities_found": result["entities_found"],
                }
                if result.get("reused"):
                    info["note"] = result.get("note")
                files_info.append(info)
                # Связь файл → сессия (частичная де-анонимизация колонок).
                # Для переиспользованных/коалесцированных файлов регистрируем
                # ИХ сессию (в ней живут маппинги, совпадающие с токенами
                # копии на диске), а не сессию этого запроса.
                anon_path = Path(result["anonymized_file"])
                result_derived = Path(_result_path_for(str(anon_path)))
                for reg_path in (Path(result["original_file"]), anon_path,
                                 result_derived):
                    await self.store.register_file_session(
                        str(reg_path), result_sid)
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
            except Exception as e:  # noqa: BLE001
                # Ошибка ОДНОГО файла (битый/недописанный DOCX/XLSX, lock-файл,
                # отказ в доступе при чтении и т.п.) не должна ронять весь
                # запрос: раньше BadZipFile отсюда улетал клиенту как
                # «Provider error: File is not a zip file» и диалог ломался
                # (багрепорт 2026-09-08). Сообщаем об ошибке файла в ответе
                # (— ОШИБКА: …) и продолжаем остальные файлы.
                logger.exception(
                    "Не удалось анонимизировать файл %s", file_path
                )
                files_info.append({"original_file": file_path, "error": str(e)})

        # Единая сессия ответа: если все файлы обработаны в одной другой
        # сессии (коалесценция параллельных дубликатов / переиспользование
        # копий), отвечаем от её имени — токены в копиях на диске
        # соответствуют маппингам именно этой сессии, иначе де-анонимизация
        # по маркерам этого ответа не найдёт значений.
        if (result_sids
                and all(s == result_sids[0] for s in result_sids)
                and result_sids[0] != session_id):
            session_id = result_sids[0]
        elif any(s != session_id for s in result_sids):
            logger.warning(
                "Смешанные сессии анонимизации в одном запросе: %s "
                "(ответ в сессии %s) — де-анонимизация файлов из чужих "
                "сессий может потребовать повторной анонимизации",
                sorted(set(result_sids)), session_id,
            )

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
            "NER-моделью. Запрос в облако НЕ отправлялся (режим manual).",
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
            if info.get("note"):
                # Переиспользованная копия (исходник не менялся): NER не
                # запускался, сущности не пересчитывались
                lines.append(
                    f"- {info['original_file']} → {info['anonymized_file']} — "
                    f"{info['note']}"
                )
            else:
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
            "<файл результата> (путь — в маркере результата выше). "
            "ВСЕ ПОСЛЕДУЮЩИЕ правки — только с --file <файл "
            "результата> и записью в него же (--output <файл результата> "
            "или --in-place): команда, начатая заново с копии, ЗАТРЁТ "
            "предыдущие правки. Анонимизированную копию не изменяйте — "
            "она исходник только для чтения (dump/list-tables).",
            "4. В конце — де-анонимизация: напишите «раскрой все данные».",
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
                f"({LOCAL_LLM['model'] or 'llama-server'}). Запросы в облако не "
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

        # 1b) Точечные команды (канонические формулировки, детерминированные
        #     правила + парсеры аргументов, БЕЗ моделей). Проверяются ДО
        #     GLiNER-слоя и обобщённого deanon_files: «Раскрой эти данные…»
        #     и «Скрой эти данные…» иначе перехватились бы полной
        #     деанонимизацией / whole-file NER соответственно.
        if content_commands:
            cmd = self._resolve_placeholder_deanon_command(request, joined)
            if cmd:
                return cmd
            cmd = self._resolve_extra_anonymize_command(request, joined)
            if cmd:
                return cmd

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
        #    де-анонимизации (каноническая команда «раскрой все данные»)
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
        if kind == "deanon_placeholders":
            return await self.handle_deanonymize_placeholders(
                request,
                command.get("tokens") or [],
                command.get("targets") or [],
                command.get("unrecognized") or [],
            )
        if kind == "extra_anonymize":
            return await self.handle_extra_anonymize(
                request,
                command.get("names") or [],
                command.get("targets") or [],
                command.get("unrecognized") or [],
            )
        if kind == "cmd_help":
            # Anti-fallthrough: команда распознана, аргументы — нет.
            return self._command_reply(
                request, command.get("text") or "", "cmd_help")
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
        по естественной команде («Раскрой все данные…»).

        Условия: в ТЕКУЩЕМ (последнем содержательном) user-сообщении есть
        команда де-анонимизации — полная («раскрой все данные») или краткая
        «раскрой …» (REVEAL_INTENT_RE; краткая форма безопасна здесь: полный
        перехват вызывается только после более строгих проверок команды,
        а точечная выборочная деанонимизация проверяется раньше; команда из
        старых сообщений истории не считается — см.
        detect_attached_files_anonymization), и в истории
        диалога есть маркеры [anonymizer:result:<путь>] с session_id.
        Основной источник — файл результата; если модель его не создала,
        де-анонимизируется анонимизированная копия (fallback).

        Returns:
            Список целей вида {"session_id", "result_path", "copy_path"}
            (пустой — запрос обрабатывается как обычно).
        """
        has_intent = any(
            DEANONYMIZE_INTENT_RE.search(text)
            or REVEAL_INTENT_RE.search(text)
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
                # Литеральные «пути» из подсказок (старые ответы содержали
                # «[anonymizer:result:<путь>]» как ТЕКСТ инструкции) не
                # являются целями: в реальных путях < и > не встречаются
                # (багрепорт 2026-09-10: фантомная цель «файл не найден»
                # из подсказки «Дальнейшие шаги» в истории диалога).
                def _real_marker_path(raw: str) -> bool:
                    p = raw.strip()
                    # Реальные пути абсолютные и не содержат < >; литералы
                    # из подсказок/пересказов («<путь>», «…») отсекаются
                    return (bool(p) and "<" not in p and ">" not in p
                            and ("\\" in p or "/" in p))

                result_paths = [
                    m.group("path").strip()
                    for m in ANONYMIZER_RESULT_MARKER_RE.finditer(text)
                    if _real_marker_path(m.group("path"))
                ]
                copy_paths = [
                    m.group("path").strip()
                    for m in ANONYMIZER_COPY_MARKER_RE.finditer(text)
                    if _real_marker_path(m.group("path"))
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
        """Текст подтверждения после де-анонимизации файлов результата.

        Заголовок вычисляется из фактических результатов: ошибки записи
        не «прячутся» за словом «де-анонимизированы» (багрепорт 2026-09-10).
        """
        errors = [i for i in files_info if "error" in i]
        if not files_info:
            header = ("[ANONYMIZER] Де-анонимизация НЕ ВЫПОЛНЕНА: не "
                      "найдено ни одного файла результата. Запрос в облако "
                      "НЕ отправлялся.")
        elif not errors:
            header = ("[ANONYMIZER] Файлы результата де-анонимизированы: "
                      "плейсхолдеры заменены реальными значениями. Запрос "
                      "в облако НЕ отправлялся.")
        elif len(errors) < len(files_info):
            header = ("[ANONYMIZER] Де-анонимизация выполнена ЧАСТИЧНО: "
                      "часть файлов НЕ обновлена (см. ошибки ниже). Запрос "
                      "в облако НЕ отправлялся.")
        else:
            header = ("[ANONYMIZER] Де-анонимизация НЕ ВЫПОЛНЕНА (см. "
                      "ошибки ниже). Запрос в облако НЕ отправлялся.")
        lines = [
            header,
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

    # ============ Точечные команды v1.11: детекция (только правила) ============

    def _resolve_placeholder_deanon_command(
        self, request: ChatCompletionRequest, joined: str,
    ) -> Optional[dict]:
        """
        Команда «Раскрой эти данные PERSON_1, PERSON_3–PERSON_5».

        Проверяется ДО обобщённого deanon_files: команда с указательным
        местоимением («эти/следующие/указанные/приведенные данные») иначе
        перехватилась бы ПОЛНОЙ деанонимизацией файлов (восстановить больше
        PII, чем попросил пользователь, нельзя). Anti-fallthrough: интент
        есть, а токены не распознаны — служебная подсказка вместо провала в
        полную деанонимизацию. Ловит и краткую форму «Раскрой PERSON_1…»
        (REVEAL_INTENT_RE, без слова «данные»): частичная деанонимизация
        безопаснее полной.
        """
        texts = last_user_message_texts(request.messages)
        explicit = any(
            PLACEHOLDER_DEANON_INTENT_RE.search(t) for t in texts)
        tokens, errors = parse_placeholder_spec(joined)
        if not explicit and not (
                tokens
                and any(DEANONYMIZE_INTENT_RE.search(t)
                        or REVEAL_INTENT_RE.search(t) for t in texts)):
            return None
        if not tokens:
            return _help_command(
                "[ANONYMIZER] Команда деанонимизации плейсхолдеров распознана, "
                "но ни одного плейсхолдера не распознано. Укажите список или "
                "диапазон, например: раскрой эти данные PERSON_1, "
                "PERSON_3–PERSON_5. Если нужна ПОЛНАЯ деанонимизация файлов — "
                "напишите «раскрой все данные». Запрос в облако НЕ отправлялся.")
        targets = self.detect_deanonymize_request(request)
        # Маркеров [anonymizer:result:…] в диалоге может не быть (новый чат),
        # но маппинги и привязки файлов сохранены в БД прокси: цели найдёт
        # фоллбек handle_deanonymize_placeholders по запрошенным токенам.
        return {
            "command": "deanon_placeholders",
            "tokens": tokens,
            "unrecognized": errors,
            "targets": targets,
        }

    def _resolve_extra_anonymize_command(
        self, request: ChatCompletionRequest, joined: str,
    ) -> Optional[dict]:
        """
        Команда «Скрой эти данные: Иванов, Петрова».

        Проверяется до перехвата авто-анонимизации приложенных файлов: у обеих
        команд общий глагол «скрой», и без приоритета точечной команды файлы
        ушли бы в whole-file NER. Anti-fallthrough: интент есть, а значения
        не распознаны — подсказка, а не whole-file NER.
        """
        match = EXTRA_ANONYMIZE_INTENT_RE.search(joined)
        if not match:
            return None
        names, errors = parse_name_list(joined[match.end():])
        if not names:
            return _help_command(
                "[ANONYMIZER] Команда дополнительной анонимизации распознана, "
                "но значения не распознаны. Перечислите их через запятую, "
                "например: скрой эти данные: Иванов, 27.12.2023, "
                "№0095/23. Если вы хотели анонимизировать приложенные файлы "
                "целиком — напишите «скрой все данные». Запрос в облако НЕ "
                "отправлялся.")
        targets, default_sid = self._detect_extra_anonymize_targets(request)
        if not targets:
            return _help_command(
                "[ANONYMIZER] Значения распознаны (" + ", ".join(names)
                + "), но в диалоге не найдено анонимизированных копий "
                "(маркеров [anonymizer:copy:…]). Сначала выполните "
                "анонимизацию файлов. Запрос в облако НЕ отправлялся.")
        return {
            "command": "extra_anonymize",
            "names": names,
            "unrecognized": errors,
            "targets": targets,
            "session_id": default_sid,
        }

    def _detect_extra_anonymize_targets(
        self, request: ChatCompletionRequest,
    ) -> tuple[list[dict], Optional[str]]:
        """
        Цели команды «Скрой эти данные…»: анонимизированные копии.

        1) явные пути в ТЕКУЩЕМ сообщении (копия используется напрямую; для
           оригинала берётся одноимённая копия <name>.anonymized.<ext>, если
           она существует);
        2) иначе — маркеры [anonymizer:copy:…] из истории диалога с
           session_id (по образцу detect_deanonymize_request).

        Returns:
            (targets, default_session_id); target = {"copy_path", "session_id"}
        """
        targets: list[dict] = []
        seen: set[str] = set()
        default_sid: Optional[str] = None

        def _add(copy_path: str, session_id: Optional[str]) -> None:
            key = norm_fs_path(copy_path)
            if key in seen:
                return
            seen.add(key)
            targets.append({"copy_path": copy_path, "session_id": session_id})

        # 1) явные пути из текущего сообщения
        for text in last_user_message_texts(request.messages):
            candidates: list[str] = []
            for m in FILE_CONTENT_BLOCK_RE.finditer(text):
                candidates.append(m.group("path").strip())
            for m in ATTACHMENT_FILE_REF_RE.finditer(text):
                candidates.append(
                    m.group("path").strip().strip('"').strip("'").strip("`"))
            for m in PATH_MENTION_RE.finditer(text):
                candidates.append(m.group(0).strip())
            for raw in candidates:
                path = _resolve_local_path(raw)
                if not path.is_file() or not FileParser.is_supported(str(path)):
                    continue
                if _is_anonymized_copy_path(raw):
                    _add(str(path), None)
                    continue
                derived = path.with_name(
                    path.stem + ".anonymized" + path.suffix)
                if derived.is_file():
                    _add(str(derived), None)

        # 2) маркеры копий из истории
        for msg in request.messages:
            for text in _iter_content_texts(msg.content):
                if "[anonymizer:copy:" not in text:
                    continue
                sid_match = SESSION_ID_LINE_RE.search(text)
                sid = sid_match.group(1) if sid_match else None
                if sid and not default_sid:
                    default_sid = sid
                for m in ANONYMIZER_COPY_MARKER_RE.finditer(text):
                    raw = m.group("path").strip()
                    # Литеральные «<путь>»/«…» из подсказок и пересказов —
                    # не цели (см. detect_deanonymize_request,
                    # багрепорт 2026-09-10)
                    if not ("<" not in raw and ">" not in raw
                            and ("\\" in raw or "/" in raw)):
                        continue
                    _add(raw, sid)

        return targets, default_sid

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

        # Manual: anonymize=False или режим manual по умолчанию
        if not request.anonymize or CURRENT_MODE == Mode.MANUAL:
            if request.anonymize and CURRENT_MODE == Mode.MANUAL:
                # Автоматическая анонимизация приложенных файлов по явной команде
                # («Скрой все данные…») — локальной NER-моделью, без облака.
                # Явное anonymize=false отключает и перехват тоже.
                # На локальном бэкенде (llama-server) перехват отключён:
                # данные не покидают машину, маскировать незачем.
                file_paths = self.detect_attached_files_anonymization(request)
                if file_paths and getattr(
                        self.openrouter, "backend", "openrouter"
                ) == "openrouter":
                    return await self.handle_files_anonymization(
                        request, file_paths, session_id
                    )
            return await self._handle_manual(request, session_id)

        # Переиспользуем prepare_chat_request (нет дублирования логики)
        prepared = await self.prepare_chat_request(request, session_id)
        session_id = prepared.session_id
        entities = prepared.entities
        mappings_dict = prepared.mappings_dict
        anonymized_messages = prepared.anonymized_messages
        original_content = prepared.original_content
        anonymized_content = prepared.anonymized_content
        canonical_result = prepared.canonical_result

        # Канонический результат сохраняется в файл: его содержимое —
        # проверяемая проекция контекста, реально уходящего в облако
        saved_path = await self._save_anonymized_request(
            session_id, canonical_result
        )

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

        # Стримим от OpenRouter.
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

    async def _handle_manual(
        self,
        request: ChatCompletionRequest,
        session_id: Optional[str] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Manual-режим (anonymize=False): отправить запрос в OpenRouter
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

        metadata: dict = {"mode": "manual"}
        if mappings_dict:
            metadata = {
                "mode": "manual_deanonymized",
                "sessions": history_sessions,
                "mappings_count": len(mappings_dict),
            }

        response = ChatCompletionResponse(
            id=cloud_response.get("id", f"manual-{int(time.time())}"),
            created=cloud_response.get("created", int(time.time())),
            model=cloud_response.get("model", request.model),
            choices=choices,
            usage=UsageInfo(**cloud_response.get("usage", {})),
            anonymization_metadata=metadata,
        )
        return response, session_id or "manual"

    async def stream_manual(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[str]:
        """
        Стриминг manual (anonymize=False): без анонимизации и
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
                        "id": f"manual-{int(time.time())}",
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

    async def handle_anonymize(
        self,
        request: AnonymizeRequest
    ) -> AnonymizeResponse:
        """
        Анонимизировать текст/файлы без отправки в облако (ручной сценарий).
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
            request_type="anonymize",
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
        mappings_subset: Optional[dict] = None,
    ) -> dict:
        """
        Де-анонимизировать файл: заменить плейсхолдеры на реальные значения
        из маппингов сессии и пересохранить файл (структура/таблицы сохраняются).

        mappings_subset — необязательное ограничение набора токенов
        (token -> original_value) для ВЫБОРОЧНОЙ де-анонимизации (команда
        «раскрой эти данные…»); None — все маппинги сессии.
        """
        if mappings_subset is None:
            mappings_dict = await self.store.get_all_mappings(session_id)
        else:
            mappings_dict = dict(mappings_subset)
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

    async def _discover_placeholder_targets(
        self, tokens: list[str]) -> list[dict]:
        """
        Фоллбек для «раскрой эти данные…»: цели из хранилища маппингов.

        Основной путь — маркеры [anonymizer:result:…] в диалоге; он не работает
        в новом чате, хотя маппинги и привязки файлов сохранены в БД прокси.
        Ищем сессии, содержащие запрошенные токены, и их зарегистрированные
        файлы (предпочитаем файлы результата, затем копии; файл должен
        существовать на диске).
        (багрепорт 2026-09-09: «POSITION_128–129» из старого диалога не
        деанонимизировались в новом чате)
        """
        targets: list[dict] = []
        try:
            sessions = await self.store.find_sessions_with_tokens(tokens)
        except Exception as exc:
            logger.warning("Поиск сессий по плейсхолдерам не удался: %s", exc)
            return targets
        for sid in sessions:
            try:
                files = await self.store.get_files_for_session(sid)
            except Exception as exc:
                logger.warning("Файлы сессии %s недоступны: %s", sid, exc)
                continue
            result_path = next(
                (f for f in files if ".result." in Path(f).name.lower()), None)
            copy_path = next(
                (f for f in files if ".anonymized." in Path(f).name.lower()),
                None)
            if result_path and not Path(result_path).is_file():
                result_path = None
            if copy_path and not Path(copy_path).is_file():
                copy_path = None
            if result_path or copy_path:
                targets.append({
                    "session_id": sid,
                    "result_path": result_path,
                    "copy_path": copy_path,
                })
        return targets

    async def handle_deanonymize_placeholders(
        self,
        request: ChatCompletionRequest,
        tokens: list[str],
        targets: list[dict],
        unrecognized: Optional[list[str]] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """
        Команда «раскрой эти данные …»: восстановить ТОЛЬКО указанные
        плейсхолдеры (список/диапазон) в файлах результата.

        Цели: маркеры [anonymizer:result:…] из диалога; если их там нет
        (например, плейсхолдеры созданы в другом чате) — фоллбек-поиск по
        хранилищу маппингов (_discover_placeholder_targets).

        Гарантия статуса файла: результат ПО-ПРЕЖНЕМУ считается анонимизированным
        (даже если восстановлены все его плейсхолдеры): анонимизированная копия
        не изменяется, маппинги сессии сохраняются, никаких маркеров
        «полностью деанонимизирован» не выставляется. Полная деанонимизация
        «насовсем» — штатная команда «раскрой все данные».
        """
        wanted = {t for t in tokens}
        targets = list(targets or [])
        if not targets:
            targets = await self._discover_placeholder_targets(tokens)
        if not targets:
            return self._command_reply(
                request,
                "[ANONYMIZER] Плейсхолдеры "
                + ", ".join(sorted(wanted))
                + " не найдены ни в одной сессии маппингов. Возможно, "
                "анонимизация ещё не выполнялась (модель загружает плейсхолдеры "
                "только для файлов, прошедших анонимизацию). Запрос в облако "
                "НЕ отправлялся.",
                "placeholders_deanonymization",
            )
        files_report: list[str] = []
        restored_pairs: list[str] = []
        unknown_tokens: set[str] = set()

        for target in targets:
            sid = target.get("session_id")
            if not sid:
                files_report.append(
                    "- (без session_id) — ОШИБКА: сессия не определена")
                continue
            result_path = target.get("result_path")
            copy_path = target.get("copy_path")
            if result_path and Path(result_path).is_file():
                file_path = result_path
                output_path = result_path  # правим файл результата на месте
            elif copy_path and Path(copy_path).is_file():
                file_path = copy_path
                # файла результата нет — как в полной деанонимизации: копию
                # не трогаем, пишем в файл результата
                output_path = _result_path_for(copy_path)
            else:
                files_report.append(
                    f"- {result_path or copy_path} — ОШИБКА: файл не найден")
                continue

            mappings_all = await self.store.get_all_mappings(sid)
            subset = {t: v for t, v in mappings_all.items() if t in wanted}
            unknown_tokens |= (wanted - set(subset))
            if not subset:
                files_report.append(
                    f"- {file_path} — указанные плейсхолдеры отсутствуют "
                    "в маппингах сессии")
                continue

            info = await self.handle_deanonymize_file(
                sid, file_path, output_path=str(output_path),
                mappings_subset=subset)
            lines = [
                f"- {info['file_path']} — ОК (восстановлено: {len(subset)})"]
            for token in sorted(subset):
                lines.append(f"  - {token} → {subset[token]}")
            if not (result_path and Path(result_path).is_file()) and copy_path:
                lines.append(
                    "  (файл результата не был создан — записан из "
                    "анонимизированной копии)")
            files_report.append("\n".join(lines))
            for token in sorted(subset):
                restored_pairs.append(f"{token} → {subset[token]}")

        unknown_sorted = sorted(unknown_tokens)
        # Заголовок вычисляется из фактических результатов: если по всем
        # целям только ошибки/отсутствия, слова «деанонимизированы» в
        # заголовке быть не должно (багрепорт 2026-09-10)
        restored_any = bool(restored_pairs)
        if restored_any:
            header = ("[ANONYMIZER] Деанонимизированы указанные "
                      "плейсхолдеры. Запрос в облако НЕ отправлялся.")
        else:
            header = ("[ANONYMIZER] Деанонимизация НЕ ВЫПОЛНЕНА: ни один "
                      "из указанных плейсхолдеров не восстановлен (см. "
                      "детали ниже). Запрос в облако НЕ отправлялся.")
        reply_lines = [
            header,
            "",
            "Файлы:",
            *files_report,
        ]
        if unrecognized:
            reply_lines.append(
                "Не распознано как плейсхолдеры (исключено): "
                + ", ".join(unrecognized))
        if unknown_sorted:
            reply_lines.append(
                "Не найдены в маппингах сессии: " + ", ".join(unknown_sorted))
        reply_lines += [
            "",
            "Важно: файл результата по-прежнему считается анонимизированным — "
            "восстановлены только перечисленные плейсхолдеры (даже если это "
            "все плейсхолдеры файла). Анонимизированная копия не изменялась; "
            "полная деанонимизация «насовсем» — командой «раскрой "
            "все данные».",
        ]
        sid_for_log = (targets[0].get("session_id")
                       if targets else "deanonymize")
        await self.store.log_request(
            session_id=sid_for_log,
            request_type="placeholders_deanonymization",
            original_content=", ".join(tokens),
            anonymized_content=", ".join(tokens),
            response_content=json.dumps(restored_pairs, ensure_ascii=False),
            entities_found=[],
            processing_time_ms=0.0,
        )
        return self._command_reply(
            request, "\n".join(reply_lines), "placeholders_deanonymization")

    async def _extra_anonymize_one_file(
        self, file_path: Path, names: list[str], sid: str,
    ) -> tuple[list[str], set[str], int]:
        """Заменить перечисленные значения в одном файле (атомарно).

        Returns:
            (строки отчёта, найденные значения, число замен)
        """
        ext = file_path.suffix.lower()
        if ext in (".txt", ".md"):
            original_bytes = None
            structure = None
            text = file_path.read_text(encoding="utf-8")
        elif ext in (".docx", ".xlsx", ".xml"):
            original_bytes = file_path.read_bytes()
            parsed = await self.file_parser.parse(
                original_bytes, file_path.name)
            text = parsed.text
            structure = parsed.structure
        else:
            raise ValueError(f"неподдерживаемый формат {ext}")

        masked = PLACEHOLDER_TOKEN_RE.sub(
            lambda m: " " * len(m.group(0)), text)
        entities: list[Entity] = []
        found: set[str] = set()
        covered_spans: set[tuple[int, int]] = set()

        def _numeric_patterns(name: str) -> list[re.Pattern]:
            """Паттерны поиска числа-подобного значения в тексте.

            Текст ячейки может отличаться от пользовательской записи формы:
            «51 903,14» в файле с числовой ячейкой — «51903.14» (str(float)).
            Перебираем варианты записи (без разрядных пробелов, запятая↔точка)
            и поглощаем дробную часть: «386887» в «386887,42» матчит ВСЁ
            число, а не оставляет обрубок «386887.[MISC_5]»
            (багрепорт 2026-09-10)."""
            pats = []
            variants = {name,
                        name.replace(" ", "").replace("\u00A0", ""),
                        name.replace(",", "."),
                        name.replace(" ", "").replace("\u00A0", "")
                        .replace(",", ".")}
            for v in variants:
                pats.append(re.compile(
                    r"(?<![0-9.,])" + re.escape(v) + r"(?:[.,]\d+)?(?![0-9])"))
            return pats

        for name in names:
            # Границы: не буква/цифра/подчёркивание — значения вида
            # «27.12.2023» и «0095/23/…» не матчятся внутри более длинных
            # номеров. Для чисел — свой набор паттернов (см. выше):
            # частичное замещение числа недопустимо
            if _NUMERIC_VALUE_RE.fullmatch(name):
                patterns = _numeric_patterns(name)
            else:
                patterns = [re.compile(
                    r"(?<![A-Za-zА-Яа-яЁё0-9_])" + re.escape(name)
                    + r"(?![A-Za-zА-Яа-яЁё0-9_])", re.IGNORECASE)]
            for pattern in patterns:
                for m in pattern.finditer(masked):
                    start, end = m.start(), m.end()
                    if any(s < end and e > start for s, e in covered_spans):
                        continue  # совпадение уже покрыто другим вариантом
                    covered_spans.add((start, end))
                    entities.append(Entity(
                        text=text[start:end],
                        type="PERSON" if is_name_like(name) else "MISC",
                        start=start, end=end, confidence=1.0))
                    found.add(name)

        async def add_mapping(original_value, entity_type, _sid=sid):
            return await self.store.add_mapping(
                _sid, original_value, entity_type)

        if entities:
            anonymized_text, mapping_entries = (
                await self.text_replacer.anonymize(
                    text, entities, add_mapping))
        else:
            anonymized_text, mapping_entries = text, []

        if ext in (".txt", ".md"):
            _atomic_write_text(file_path, anonymized_text)
        else:
            assembled = await self.file_assembler.assemble(
                original_bytes, file_path.name, anonymized_text,
                structure, strip_hf_images=False, scrub_metadata=False)
            _atomic_write_bytes(file_path, assembled)

        lines = [f"- {file_path} — ОК (замен: {len(entities)})"]
        seen_pairs: set[str] = set()
        for entry in mapping_entries:
            pair = f"{entry.original_value} → {entry.token}"
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                lines.append(f"  - {pair}")
        if not mapping_entries:
            lines.append("  - вхождения не найдены")
        return lines, found, len(entities)

    async def handle_extra_anonymize(
        self,
        request: ChatCompletionRequest,
        names: list[str],
        targets: list[dict],
        unrecognized: Optional[list[str]] = None,
    ) -> tuple[ChatCompletionResponse, str]:
        """Команда «скрой эти данные: …»: заменить в копиях
        и файлах результата строки, пропущенные NER (перечислены
        пользователем): имена, номера договоров, даты, суммы — любые строки
        с буквой/цифрой.

        Детерминированно, без облака и без NER: вхождения — регистро-
        независимый поиск по маске (существующие плейсхолдеры исключены),
        для каждой найденной формы — маппинг сессии. Файлы перезаписываются
        атомарно на месте (артефакты прокси); исходник не изменяется, файл
        по-прежнему считается анонимизированным. Тип плейсхолдера: PERSON
        для значений, похожих на имя человека, MISC — для остальных
        (номера, даты и т.п.). Значение, добавленное агентом после
        анонимизации (есть только в result), заменяется в result — копия
        остаётся снимком «оригинал минус PII».
        """
        default_sid = None
        for target in targets:
            if target.get("session_id"):
                default_sid = target["session_id"]
                break
        if not default_sid and targets:
            default_sid = await self.store.get_latest_session_for_file(
                targets[0]["copy_path"])
        if not default_sid:
            default_sid = await self.store.get_or_create_session(None)

        files_report: list[str] = []
        copy_markers: list[str] = []
        not_found_everywhere = list(names)
        all_entities_count = 0
        # Файловый уровень успеха/провала — заголовок считается по ФАЙЛАМ:
        # «копия ОК + занятый result» — это ЧАСТИЧНЫЙ успех, а не провал
        any_file_ok = False
        any_file_failed = False

        for target in targets:
            copy_path = Path(target["copy_path"])
            sid = target.get("session_id") or default_sid
            if not copy_path.is_file():
                files_report.append(f"- {copy_path} — ОШИБКА: файл не найден")
                any_file_failed = True
                continue

            # Вариант A: замены применяются к ОБОИМ файлам — копии и файлу
            # результата (если агент его уже создал). Значение, добавленное
            # агентом после анонимизации, есть только в result — оно
            # заменяется там; копия при этом остаётся консистентным снимком
            # «оригинал минус PII».
            result_path = Path(_result_path_for(str(copy_path)))
            files_to_process = [copy_path]
            if result_path.is_file():
                files_to_process.append(result_path)

            found_in_copy: set[str] = set()
            found_overall: set[str] = set()
            replaces_total = 0
            failed_files: list[Path] = []
            for i, file_path in enumerate(files_to_process):
                try:
                    lines, found, replaces = (
                        await self._extra_anonymize_one_file(
                            file_path, names, sid))
                except Exception as exc:
                    # Ошибка одного файла не глотается молча и не валит
                    # остальные: честная строка в отчёте по каждому файлу
                    # (багрепорт 2026-09-10: занятый Word-ом result давал
                    # ответ «выполнена» при незаписанном файле)
                    logger.exception(
                        "Ошибка дополнительной анонимизации %s", file_path)
                    files_report.append(f"- {file_path} — ОШИБКА: {exc}")
                    failed_files.append(file_path)
                    continue
                files_report.extend(lines)
                found_overall |= found
                replaces_total += replaces
                if i == 0:
                    found_in_copy = found

            if failed_files:
                any_file_failed = True
                if len(failed_files) < len(files_to_process):
                    any_file_ok = True
                    files_report.append(
                        "  - ВНИМАНИЕ: обновлены не все файлы (см. ошибки "
                        "выше) — состояние файлов НЕконсистентно, повторите "
                        "команду после устранения причины")
            else:
                any_file_ok = True
                all_entities_count += replaces_total
                # Честная пометка: значения, которых нет в копии (появились
                # после анонимизации) — заменены только в файле результата
                only_in_result = sorted(found_overall - found_in_copy)
                if only_in_result:
                    files_report.append(
                        "  - Примечание: " + ", ".join(only_in_result)
                        + " — в анонимизированной копии отсутствуют "
                        "(появились после анонимизации); заменены только "
                        "в файле результата.")
                for name in found_overall:
                    if name in not_found_everywhere:
                        not_found_everywhere.remove(name)
                copy_markers.append(f"[anonymizer:copy:{copy_path}]")

        # Заголовок вычисляется из фактических результатов, а не пишется
        # константой: иначе ошибки записи «прячутся» за словом «выполнена»
        if any_file_failed and any_file_ok:
            header = ("[ANONYMIZER] Дополнительная анонимизация выполнена "
                      "ЧАСТИЧНО: часть файлов НЕ обновлена (см. ошибки "
                      "ниже). Запрос в облако НЕ отправлялся.")
        elif any_file_failed:
            header = ("[ANONYMIZER] Дополнительная анонимизация НЕ "
                      "ВЫПОЛНЕНА (см. ошибки ниже). Запрос в облако НЕ "
                      "отправлялся.")
        elif all_entities_count > 0:
            header = ("[ANONYMIZER] Дополнительная анонимизация "
                      "выполнена: указанные значения заменены "
                      "плейсхолдерами. Запрос в облако НЕ отправлялся.")
        else:
            header = ("[ANONYMIZER] Дополнительная анонимизация "
                      "завершена, но ни одно из указанных значений не "
                      "найдено в файлах — замены не выполнялись. "
                      "Запрос в облако НЕ отправлялся.")

        reply_lines = [
            header,
            "",
            "Файлы:",
            *files_report,
        ]
        if not_found_everywhere:
            reply_lines.append("")
            reply_lines.append(
                "Не найдено в файлах (регистр не важен; укажите форму слова "
                "как в файле): " + ", ".join(not_found_everywhere))
        if unrecognized:
            reply_lines.append(
                "Не распознано как значения (исключено из обработки): "
                + ", ".join(unrecognized))
        reply_lines += [
            "",
            f"session_id: {default_sid}",
            "Файл по-прежнему считается анонимизированным; исходный файл "
            "не изменялся.",
            *copy_markers,
        ]
        await self.store.log_request(
            session_id=default_sid,
            request_type="extra_anonymization",
            original_content=", ".join(names),
            anonymized_content=json.dumps(files_report, ensure_ascii=False),
            response_content=None,
            entities_found=[],
            processing_time_ms=0.0,
        )
        return self._command_reply(
            request, "\n".join(reply_lines), "extra_anonymize")

    async def handle_anonymize_file(
        self,
        file_path: str,
        session_id: Optional[str] = None,
        output_path: Optional[str] = None,
        create_review_md: bool = True,
        allow_reuse: bool = True,
    ) -> dict:
        """
        Анонимизировать локальный файл: создать анонимизированную копию
        (структура/таблицы сохраняются) рядом с оригиналом.

        create_review_md=False — не сохранять .md-предпросмотр (перехват в
        manual-режиме: пользователь правит копию в исходном формате).

        allow_reuse=False — не переиспользовать существующую копию, даже если
        исходник не менялся (используется в смешанных запросах, где часть
        файлов свежая, а часть уже анонимизирована: консистентность маппингов
        одной сессии важнее экономии времени).
        """
        session_id = await self.store.get_or_create_session(session_id)
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        if not FileParser.is_supported(path.name):
            raise ValueError(f"Неподдерживаемый формат файла: {path.name}")

        # Переиспользование: копия уже существует и исходник с тех пор не
        # менялся — повторный NER-прогон (десятки секунд на больших файлах)
        # не нужен, возвращаем существующую копию в её исходной сессии.
        # (багрепорт 2026-09-08: повторные запросы Hermes — основной чат +
        # вспомогательные title_generation с той же историей — по 4 раза
        # гоняли NER по одному и тому же файлу)
        if allow_reuse and output_path is None:
            reused = self._reusable_copy(path)
            if reused is not None:
                # Копия переиспользуется только если создана ТЕКУЩЕЙ версией
                # парсера: копии, сделанные до обновления (например, без
                # поддержки вложенных таблиц/комментариев), пересоздаются
                marker = read_anon_marker(reused)
                if path.suffix.lower() in (".docx", ".xlsx") \
                        and marker != ANON_MARKER:
                    logger.info(
                        "Копия %s создана другой версией анонимизатора "
                        "(маркер: %r) — требуется переанонимизация",
                        reused, marker,
                    )
                else:
                    old_sid = (
                        await self.store.get_latest_session_for_file(str(path))
                        or await self.store.get_latest_session_for_file(str(reused))
                        or session_id
                    )
                    logger.info(
                        "Файл %s уже анонимизирован (исходник не менялся) — "
                        "копия %s переиспользуется (сессия %s)",
                        path, reused, old_sid,
                    )
                    return {
                        "session_id": old_sid,
                        "original_file": str(path),
                        "anonymized_file": str(reused),
                        "review_file": None,
                        "anonymized_markdown": "",
                        "entities_found": 0,
                        "mappings_count": len(
                            await self.store.get_all_mappings(old_sid)
                        ),
                        "reused": True,
                        "note": (
                            "копия уже существовала, исходник не менялся — "
                            "переиспользована без повторного NER-прогона"
                        ),
                    }

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
            reason = getattr(self.ner, "last_error", "")
            raise NERUnavailableError(
                "NER-модель не загрузилась или не вернула результат"
                + (f" ({reason})" if reason else "")
                + " — анонимизация файла не выполнена"
            )

        # Анонимизируем ПОСЕГМЕНТНО: NER-сущность может пересекать границу
        # строк склеенного текста (например, «Sasha\nА.В. Гершойг» — автор и
        # текст комментария в соседних сегментах; «АП\nОбособленные
        # подразделения» — многоабзацная ячейка). Замена такой сущности в
        # склеенном тексте удаляет «\n», число строк анонимизированного
        # текста перестаёт совпадать с числом сегментов, и хвост сегментов
        # (вложенные таблицы, комментарии) оставался без замен — сборка
        # идёт по строгому соответствию «строка ↔ сегмент» (zip).
        # (багрепорт 2026-09-08: PII на титуле и в комментариях утекала,
        # хотя маппинги были созданы)
        segments = parsed.text.split("\n")
        segment_entities = split_entities_by_segments(
            entities, segments, separator="\n"
        )
        anon_lines: list[str] = []
        mappings = []
        for line, line_ents in zip(segments, segment_entities):
            anon_line, line_maps = await self.text_replacer.anonymize(
                line, line_ents, add_mapping
            )
            anon_lines.append(anon_line)
            mappings.extend(line_maps)
        anon_text = "\n".join(anon_lines)
        anon_text = "\n".join(anon_lines)
        anon_content = await self.file_assembler.assemble(
            content, path.name, anon_text, parsed.structure
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