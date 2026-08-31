"""
GLiNER NER-движок — in-process NER без внешнего сервера.

Бэкенд инференса выбирается автоматически (NER_BACKEND в .env):
- onnx (auto, по умолчанию): ONNX Runtime с автовыбором провайдера
  CUDA → DirectML → CPU — один и тот же model.onnx исполняется на любом
  железе; на машинах с GPU инференс ускоряется в разы без правки кода;
- torch: PyTorch (аварийный фоллбек, если ONNX не загрузился).

Модель загружается лениво при первом вызове; инференс выполняется
в отдельном потоке (ThreadPoolExecutor), чтобы не блокировать event-loop.
Для длинных текстов применяется чанкование с перекрытием.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Tuple, Optional

from ..models.schemas import Entity
from ..config import NER_ENGINE

logger = logging.getLogger("anonymizer_proxy.ner_engine")

# ── Маппинг категорий проекта → англоязычные метки GLiNER ──────────────
DEFAULT_LABEL_MAP: Dict[str, str] = {
    "PERSON":     "person name",
    "SURNAME":    "surname or last name or family name",
    "POSITION":   "job position or title",
    "DEPARTMENT": "department or organizational unit",
    "ORG":        "organization name",
    "LOC":        "location or address",
    "PRODUCT":    "product name or software name",
    "PASSPORT":   "passport number",
    "PHONE":      "phone number",
    "EMAIL":      "email address",
    "WEB":        "website address or URL",
    "INN":        "tax identification number",
    "MONEY":      "money amount",
}

# Возможные расположения ONNX-весей в репозитории модели (пробуются по порядку)
_ONNX_MODEL_FILES: Tuple[str, ...] = ("onnx/model.onnx", "model.onnx")


def detect_onnx_providers() -> List[str]:
    """
    Автодетекция провайдера ONNX Runtime под текущее железо.

    Порядок: CUDA (NVIDIA) → DirectML (AMD/Intel GPU, NPU) → CPU.
    CPUExecutionProvider всегда идёт последним как фоллбек самого ORT.
    Пустой список — onnxruntime не установлен (движок откатится на PyTorch).
    """
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]
    except ImportError:
        return []
    available = ort.get_available_providers()
    providers: List[str] = []
    for gpu_provider in ("CUDAExecutionProvider", "DmlExecutionProvider"):
        if gpu_provider in available:
            providers.append(gpu_provider)
            break
    providers.append("CPUExecutionProvider")
    return providers


def _register_nvidia_pip_dlls() -> None:
    """
    Windows: подключить CUDA/cuDNN DLL из pip-пакетов nvidia-*-cu12/cu13.

    Пакеты nvidia-cublas-cu12 и nvidia-cudnn-cu12 кладут DLL в
    site-packages/nvidia/<lib>/bin, но onnxruntime ищет их по PATH.
    Регистрируем эти каталоги через add_dll_directory + PATH, чтобы
    CUDAExecutionProvider поднимался без системной установки CUDA Toolkit.
    Ничего не делает, если пакетов нет (например, CPU/DirectML-конфигурация).
    """
    import sys

    if sys.platform != "win32":
        return
    import site
    import os

    roots: list[str] = []
    for prefix in site.getsitepackages():
        roots.append(prefix)
    if getattr(sys, "prefix", ""):
        roots.append(os.path.join(sys.prefix, "Lib", "site-packages"))

    for root in roots:
        nvidia_dir = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia_dir):
            continue
        for lib_name in sorted(os.listdir(nvidia_dir)):
            bin_dir = os.path.join(nvidia_dir, lib_name, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                except OSError:
                    pass


class GlinerEngine:
    """Ленивая обёртка над GLiNER-моделью."""

    def __init__(
        self,
        model_name: str | None = None,
        threshold: float | None = None,
        device: str | None = None,
        label_map: Dict[str, str] | None = None,
        chunk_overlap_chars: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._model_name: str = model_name or NER_ENGINE["model"]
        self._threshold: float = threshold if threshold is not None else NER_ENGINE["threshold"]
        self._device: str = device or NER_ENGINE["device"]
        self._label_map: Dict[str, str] = label_map or DEFAULT_LABEL_MAP
        self._reverse_map: Dict[str, str] = {v: k for k, v in self._label_map.items()}
        # Перекрытие берётся из конфига (NER_CHUNK_OVERLAP_CHARS), если не
        # задано явно (тесты). Раньше здесь был зашитый дефолт 500, из-за
        # которого настройка в config.py не работала, а треть текста
        # обрабатывалась моделью дважды.
        if chunk_overlap_chars is None:
            chunk_overlap_chars = int(NER_ENGINE.get("chunk_overlap_chars", 200))
        self._chunk_overlap: int = max(0, min(chunk_overlap_chars, NER_ENGINE["max_input_chars"] // 2))
        self._timeout: float = float(
            timeout_seconds if timeout_seconds is not None else NER_ENGINE.get("timeout", 300)
        )

        self._model: object | None = None
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gliner"
        )
        self._loaded: bool = False
        self._load_error: str | None = None
        # Фактический бэкенд и провайдеры (заполняются при загрузке; для /health)
        self._backend: str = ""        # "onnx" | "torch"
        self._providers: List[str] = []
        self._max_token_length: int = 512  # default, переопределяется после загрузки
        self._safe_chunk_chars: int = 1536  # ~512 токенов × 3 символа/токен (русский)


    # ── загрузка ────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        backend = str(NER_ENGINE.get("backend", "auto")).strip().lower()
        if backend != "torch":
            try:
                self._load_onnx()
                return
            except Exception as exc:
                # Деградация должна быть видимой, а не тихой
                logger.warning(
                    "NER: ONNX-бэкенд не загрузился (%s). Откат на PyTorch — "
                    "это медленнее. Проверьте установку onnxruntime "
                    "(pip install onnxruntime | onnxruntime-directml | onnxruntime-gpu)",
                    exc,
                )
                self._load_error = str(exc)
        self._load_torch()

    def _load_onnx(self) -> None:
        """Загрузить модель через ONNX Runtime с автовыбором провайдера."""
        import onnxruntime as ort
        from gliner import GLiNER  # type: ignore[import-untyped]

        _register_nvidia_pip_dlls()

        detected_providers = detect_onnx_providers()
        if not detected_providers:
            raise ImportError("onnxruntime не установлен")

        logger.info(
            "Загрузка GLiNER ONNX %s (провайдер: %s)…",
            self._model_name, detected_providers[0],
        )

        # gliner при создании ONNX-сессии хардкодит CPUExecutionProvider.
        # Подменяем конструктор сессии на время загрузки, чтобы подставить
        # автодетектированный список провайдеров; если GPU-провайдер не
        # инициализировался (например, драйвер), сессия пересоздаётся на CPU.
        # Патч строго ограничен этим методом: finally восстанавливает оригинал.
        original_session = ort.InferenceSession

        def _session_with_providers(path_or_bytes, sess_options=None, providers=None, **kwargs):
            try:
                return original_session(
                    path_or_bytes, sess_options,
                    providers=detected_providers, **kwargs,
                )
            except Exception:
                if detected_providers != ["CPUExecutionProvider"]:
                    logger.warning(
                        "NER: GPU-провайдер %s не инициализировался — "
                        "сессия создана на CPU",
                        detected_providers,
                    )
                    return original_session(
                        path_or_bytes, sess_options,
                        providers=["CPUExecutionProvider"], **kwargs,
                    )
                raise

        last_error: Exception | None = None
        ort.InferenceSession = _session_with_providers
        try:
            for rel_path in _ONNX_MODEL_FILES:
                try:
                    self._model = GLiNER.from_pretrained(
                        self._model_name,
                        load_tokenizer=True,
                        load_onnx_model=True,
                        onnx_model_file=rel_path,
                    )
                    break
                except FileNotFoundError as exc:
                    last_error = exc
                    continue
        finally:
            ort.InferenceSession = original_session

        if self._model is None:
            raise FileNotFoundError(
                f"ONNX-веси модели не найдены ({', '.join(_ONNX_MODEL_FILES)}): {last_error}"
            )

        self._backend = "onnx"
        try:
            self._providers = list(self._model.session.get_providers())
        except Exception:  # нестандартная ORT-обёртка — берём запрошенный список
            self._providers = list(detected_providers)
        self._finalize_load()
        logger.info(
            "GLiNER ONNX загружен (провайдеры: %s, max_len=%d токенов)",
            ", ".join(self._providers), self._max_token_length,
        )

    def _load_torch(self) -> None:
        """Загрузить модель через PyTorch (NER_BACKEND=torch или фоллбек)."""
        from gliner import GLiNER  # type: ignore[import-untyped]

        logger.info(
            "Загрузка GLiNER (PyTorch) %s (device=%s)…",
            self._model_name, self._device,
        )
        try:
            self._model = GLiNER.from_pretrained(
                self._model_name,
                map_location=self._device,
                load_tokenizer=True,
            )
        except Exception as exc:
            self._load_error = str(exc)
            logger.error("Ошибка загрузки GLiNER-модели: %s", exc)
            raise
        self._backend = "torch"
        self._providers = []
        self._finalize_load()
        logger.info(
            "GLiNER-модель загружена (max_len=%d токенов)",
            self._max_token_length,
        )

    def _finalize_load(self) -> None:
        """Общая пост-инициализация после загрузки любого бэкенда."""
        cfg = self._model.config
        self._max_token_length = getattr(cfg, "max_len", 512) or 512
        if self._max_token_length < 128:
            self._max_token_length = 512
        # Безопасный лимит чанка в символах: окно токенов × ~3 символа/токен
        # (для русского текста). Защищает от тихого усечения моделью.
        self._safe_chunk_chars = max(500, self._max_token_length * 3)
        self._loaded = True
        self._load_error = None

    def describe(self) -> str:
        """Человекочитаемое описание бэкенда (для /health и лога старта)."""
        if not self._loaded:
            return "не загружен"
        if self._backend == "onnx":
            return f"onnx ({', '.join(self._providers)})"
        return f"torch ({self._device})"

    def is_available(self) -> bool:
        """Проверить, загружена ли модель (без попытки загрузки)."""
        return self._loaded

    async def warmup(self) -> None:
        """Прогреть модель: загрузить веса в фоне."""
        await asyncio.get_running_loop().run_in_executor(
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
        total = len(chunks)
        started = time.monotonic()

        for i, (chunk_text, offset) in enumerate(chunks, start=1):
            raw: List[dict] = self._model.predict_entities(  # type: ignore[union-attr]
                text=chunk_text,
                labels=labels,
                flat_ner=True,
                threshold=self._threshold,
            )

            # Прогресс: большие документы обрабатываются минутами; без этих
            # строк обработка неотличима от зависания (из-за чего прокси
            # останавливали вручную)
            if total > 1 and (i % 10 == 0 or i == total):
                logger.info(
                    "NER: чанк %d/%d (%.0fс прошло)",
                    i, total, time.monotonic() - started,
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
        """
        Асинхронный инференс: синхронный вызов в executor-потоке.

        Страховочный таймаут NER_TIMEOUT_SECONDS: раньше настройка
        объявлялась в конфиге, но нигде не применялась — зависший
        инференс мог держать запрос бесконечно долго.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, self._predict, text, categories)
        if self._timeout and self._timeout > 0:
            try:
                return await asyncio.wait_for(future, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"NER-инференс не уложился в {self._timeout:.0f} с "
                    "(NER_TIMEOUT_SECONDS)"
                ) from exc
        return await future

    async def predict_with_labels(
        self, text: str, labels: List[str], threshold: float = 0.0,
        timeout: Optional[float] = None,
    ) -> List[dict]:
        """
        Zero-shot инференс с произвольными метками (вне PII-схемы).

        Используется ИИ-детектором чат-команд: GLiNER сравнивает текст с
        классами-описаниями («команда переключения на облако» и т.п.).
        Чанкование не нужно — детектируются короткие сообщения; инференс
        выполняется в том же executor-потоке, что и основной NER-путь.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executor, self._predict_raw, text, labels, threshold,
        )
        effective_timeout = timeout if timeout and timeout > 0 else self._timeout
        if effective_timeout and effective_timeout > 0:
            try:
                return await asyncio.wait_for(future, timeout=effective_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "GLiNER: таймаут predict_with_labels (%.0f с)",
                    effective_timeout,
                )
                return []
        return await future

    def _predict_raw(
        self, text: str, labels: List[str], threshold: float,
    ) -> List[dict]:
        """Синхронный инференс с сырыми метками (без маппинга категорий)."""
        self._ensure_loaded()
        if not self._model or not labels:
            return []
        return list(self._model.predict_entities(
            text=text,
            labels=list(labels),
            flat_ner=True,
            threshold=max(0.0, float(threshold)),
        ))

    async def close(self) -> None:
        """Освободить ресурсы."""
        self._executor.shutdown(wait=False)
        self._model = None
        self._loaded = False

