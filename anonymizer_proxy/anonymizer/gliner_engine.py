"""
GLiNER NER-движок — in-process замена LM Studio.

Модель загружается лениво при первом вызове; инференс выполняется
в отдельном потоке (ThreadPoolExecutor), чтобы не блокировать event-loop.
Для длинных текстов применяется чанкование с перекрытием.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Tuple

from ..models.schemas import Entity
from ..config import NER_ENGINE

logger = logging.getLogger("anonymizer_proxy.ner_engine")

# ── Маппинг категорий проекта → англоязычные метки GLiNER ──────────────
DEFAULT_LABEL_MAP: Dict[str, str] = {
    "PERSON":     "person name",
    "POSITION":   "job position or title",
    "DEPARTMENT": "department or organizational unit",
    "ORG":        "organization name",
    "LOC":        "location or address",
    "PRODUCT":    "product name or software name",
    "PASSPORT":   "passport number",
    "PHONE":      "phone number",
    "EMAIL":      "email address",
    "INN":        "tax identification number",
    "MONEY":      "money amount",
}


class GlinerEngine:
    """Ленивая обёртка над GLiNER-моделью."""

    def __init__(
        self,
        model_name: str | None = None,
        threshold: float | None = None,
        device: str | None = None,
        label_map: Dict[str, str] | None = None,
        chunk_overlap_chars: int = 500,
        timeout_seconds: float = 300,
    ) -> None:
        self._model_name: str = model_name or NER_ENGINE["model"]
        self._threshold: float = threshold if threshold is not None else NER_ENGINE["threshold"]
        self._device: str = device or NER_ENGINE["device"]
        self._label_map: Dict[str, str] = label_map or DEFAULT_LABEL_MAP
        self._reverse_map: Dict[str, str] = {v: k for k, v in self._label_map.items()}
        self._chunk_overlap: int = max(0, min(chunk_overlap_chars, NER_ENGINE["max_input_chars"] // 2))
        self._timeout: float = float(timeout_seconds)

        self._model: object | None = None
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gliner"
        )
        self._loaded: bool = False
        self._load_error: str | None = None
        self._max_token_length: int = 512  # default, переопределяется после загрузки
        self._safe_chunk_chars: int = 1536  # ~512 токенов × 3 символа/токен (русский)

    # ── загрузка ────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            from gliner import GLiNER  # type: ignore[import-untyped]

            logger.info(
                "Загрузка GLiNER-модели %s (device=%s)…",
                self._model_name, self._device,
            )
            self._model = GLiNER.from_pretrained(
                self._model_name,
                map_location=self._device,
                load_tokenizer=True,
            )
            cfg = self._model.config
            self._max_token_length = getattr(cfg, "max_len", 512) or 512
            if self._max_token_length < 128:
                self._max_token_length = 512
            # Безопасный лимит чанка в символах: окно токенов × ~3 символа/токен
            # (для русского текста). Защищает от тихого усечения моделью.
            self._safe_chunk_chars = max(500, self._max_token_length * 3)
            logger.info(
                "GLiNER-модель загружена (max_len=%d токенов)",
                self._max_token_length,
            )
            self._loaded = True
        except Exception as exc:
            self._load_error = str(exc)
            logger.error("Ошибка загрузки GLiNER-модели: %s", exc)
            raise

    def is_available(self) -> bool:
        """Проверить, загружена ли модель (без попытки загрузки)."""
        return self._loaded

    async def warmup(self) -> None:
        """Прогреть модель: загрузить веса в фоне."""
        await asyncio.get_event_loop().run_in_executor(
            self._executor, self._ensure_loaded
        )

    # ── чанкование ──────────────────────────────────────────────────────

    def _split_into_chunks(self, text: str) -> List[Tuple[str, int]]:
        """Разбить текст на чанки с перекрытием по границам строк."""
        # Не превышаем ни настроенный лимит, ни безопасный лимит модели
        max_chars = min(NER_ENGINE["max_input_chars"], self._safe_chunk_chars)
        if len(text) <= max_chars:
            return [(text, 0)]

        overlap = min(self._chunk_overlap, max_chars // 2)
        chunks: List[Tuple[str, int]] = []
        total = len(text)
        start = 0
        while start < total:
            end = min(start + max_chars, total)
            if end < total:
                # Ищем перевод строки в последних 20% чанка
                window_start = start + max_chars * 4 // 5
                newline_pos = text.rfind("\n", window_start, end)
                if newline_pos > start:
                    end = newline_pos + 1
            chunks.append((text[start:end], start))
            if end >= total:
                break
            start = end - overlap
        return chunks

    # ── инференс ────────────────────────────────────────────────────────

    def _predict(self, text: str, categories: List[str]) -> List[Entity]:
        """Синхронный инференс (вызывается в executor-потоке)."""
        self._ensure_loaded()

        labels = [self._label_map.get(c, c) for c in categories if c in self._label_map]
        if not labels:
            return []

        chunks = self._split_into_chunks(text)
        all_entities: List[Entity] = []
        seen: set = set()

        for chunk_text, offset in chunks:
            if len(chunks) > 1:
                logger.debug("Чанк %d символов @%d", len(chunk_text), offset)

            raw: List[dict] = self._model.predict_entities(  # type: ignore[union-attr]
                text=chunk_text,
                labels=labels,
                flat_ner=True,
                threshold=self._threshold,
            )

            for ent in raw:
                cat = self._reverse_map.get(ent["label"], ent["label"])
                start = ent["start"] + offset
                end = ent["end"] + offset
                matched = text[start:end]
                key = (start, end, matched, cat)
                if key in seen:
                    continue  # дубль из зоны перекрытия
                seen.add(key)
                all_entities.append(Entity(
                    text=matched,
                    type=cat,
                    start=start,
                    end=end,
                    confidence=float(ent.get("score", 0.0)),
                ))

        all_entities.sort(key=lambda e: e.start)
        return all_entities

    async def predict(self, text: str, categories: List[str]) -> List[Entity]:
        """Асинхронный инференс: синхронный вызов в executor-потоке."""
        return await asyncio.get_event_loop().run_in_executor(
            self._executor,
            self._predict,
            text,
            categories,
        )

    async def close(self) -> None:
        """Освободить ресурсы."""
        self._executor.shutdown(wait=False)
        self._model = None
        self._loaded = False

