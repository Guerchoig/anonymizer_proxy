"""
Роутер LLM-бэкендов: OpenRouter (облако) и локальная модель (LM Studio).

Интерфейс совпадает с OpenRouterClient (chat_completion,
chat_completion_stream, get_models, close), поэтому RequestHandler и
main.py работают с роутером без изменений.

Выбор бэкенда на каждый запрос (по приоритету):
1. Явный backend-аргумент (заголовок X-LLM-Backend из main.py);
2. Имя модели в запросе: "local/..." или точное имя локальной модели
   (LOCAL_LLM["model"]) -> local; "cloud/..." -> openrouter;
3. Рантайм-дефолт RUNTIME["backend"] — переключается чат-командой
   («работай через локальную модель» / «…через облако») или
   POST /api/backend БЕЗ перезапуска сервера; выбор сохраняется
   в data/runtime_state.json.

Для локального бэкенда из ответа вырезается thinking-вывод модели
(<think>…</think> и reasoning_content) — Cline получает чистый текст.
"""
import json
import re
from typing import AsyncIterator, Optional

import httpx

from ..config import (
    LOCAL_LLM, OPENROUTER, RUNTIME, load_runtime_state, logger,
    save_runtime_state,
)
from .openrouter_client import OpenRouterClient, OpenRouterError

_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL)  # незакрытый блок

_LEN_THINK_OPEN = len("<think>")
_LEN_THINK_CLOSE = len("</think>")


def strip_thinking(text: str) -> str:
    """Вырезать <think>…</think> (в т.ч. незакрытый) из текста ответа."""
    if not text:
        return text
    text = _THINK_CLOSED_RE.sub("", text)
    return _THINK_OPEN_RE.sub("", text)


class _ThinkFilter:
    """Потоковый фильтр thinking-вывода.

    Вырезает <think>…</think> из SSE-потока, корректно обрабатывая теги,
    разорванные между чанками (буферизует возможное начало/хвост тега).
    """

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._inside:
                j = self._buf.find("</think>")
                if j == -1:
                    # отдаём всё, кроме возможного хвоста "</thi..."
                    safe = max(0, len(self._buf) - _LEN_THINK_CLOSE + 1)
                    if safe:
                        out.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    return "".join(out)
                self._buf = self._buf[j + _LEN_THINK_CLOSE:]
                self._inside = False
                continue
            i = self._buf.find("<think>")
            if i == -1:
                safe = max(0, len(self._buf) - _LEN_THINK_OPEN + 1)
                if safe:
                    out.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                return "".join(out)
            out.append(self._buf[:i])
            self._buf = self._buf[i + _LEN_THINK_OPEN:]
            self._inside = True

    def flush(self) -> str:
        """Хвост потока: незакрытый <think> до конца — вывод отбрасывается."""
        tail = "" if self._inside else self._buf
        self._buf = ""
        return tail


class LocalLMClient:
    """Клиент локальной модели (LM Studio, OpenAI-совместимый API)."""

    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.api_key = config.get("api_key") or "lm-studio"
        self.model = config.get("model") or ""
        self.timeout = config.get("timeout", 600.0)
        # Минимальный бюджет выходных токенов для локальной модели.
        # Клиент (Cline) может прислать маленький max_tokens — на thinking-
        # модели лимит съедается размышлениями, и текст обрывается на
        # полуслове. Локальная генерация бесплатна, контекст большой —
        # поэтому поднимаем бюджет до этого минимума.
        self.min_max_tokens = int(config.get("min_max_tokens", 16384))
        self._client: Optional[httpx.AsyncClient] = None
        self._think = _ThinkFilter()
        self._resolved_model: Optional[str] = None

    @staticmethod
    def _effective_max_tokens(max_tokens: Optional[int], floor: int) -> Optional[int]:
        """Поднять max_tokens клиента до минимума (None — не трогаем)."""
        if max_tokens is None:
            return None
        return max(max_tokens, floor)

    async def _resolve_model(self, requested: Optional[str]) -> str:
        """Имя модели для запроса: явное > из .env > первая из LM Studio.

        Fallback нужен, чтобы связка работала «из коробки»: если
        LOCAL_LLM_MODEL не задан, берётся первая загруженная в LM Studio
        не-embedding модель.
        """
        if requested:
            return requested
        if self.model:
            return self.model
        if self._resolved_model is None:
            models = await self.list_models()
            usable = [m.get("id") for m in models
                      if m.get("id") and "embed" not in m.get("id", "").lower()]
            self._resolved_model = usable[0] if usable else ""
            if self._resolved_model:
                logger.info(
                    "LOCAL_LLM_MODEL не задан — использую модель из "
                    "LM Studio: %s", self._resolved_model)
        return self._resolved_model

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _unavailable_hint(self) -> str:
        return (
            "Локальная модель недоступна: LM Studio не запущен или сервер "
            f"не отвечает ({self.base_url}). Запустите LM Studio, загрузите "
            f"модель {self.model or '(см. .env LOCAL_LLM_MODEL)'} и "
            "повторите запрос, либо переключитесь на облако командой "
            "«работай через облако»."
        )

    def _strip_choice(self, choice: dict) -> None:
        message = choice.get("message") or {}
        message.pop("reasoning_content", None)
        if isinstance(message.get("content"), str):
            message["content"] = strip_thinking(message["content"])

    def _strip_delta(self, delta: dict) -> None:
        delta.pop("reasoning_content", None)
        if isinstance(delta.get("content"), str) and delta["content"]:
            delta["content"] = self._think.feed(delta["content"])

    @staticmethod
    def _extract_error(error_text: str) -> str:
        try:
            return json.loads(error_text).get("error", {}).get(
                "message", error_text)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return error_text

    async def chat_completion(
        self, messages: list[dict], model: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
        **kwargs,
    ) -> dict:
        client = await self._get_client()
        payload: dict = {"model": await self._resolve_model(model), "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            # локальная генерация бесплатна: поднимаем лимит клиента до минимума
            payload["max_tokens"] = self._effective_max_tokens(
                max_tokens, self.min_max_tokens)
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value

        try:
            response = await client.post(
                f"{self.base_url}/chat/completions", json=payload)
        except httpx.ConnectError as exc:
            raise OpenRouterError(502, self._unavailable_hint()) from exc

        if response.status_code != 200:
            error_text = self._extract_error(response.text)
            logger.error("Ошибка локальной модели %s: %s",
                         response.status_code, error_text[:500])
            raise OpenRouterError(response.status_code, error_text)

        data = response.json()
        for choice in data.get("choices", []):
            self._strip_choice(choice)
            if choice.get("finish_reason") == "length":
                logger.warning(
                    "Локальная модель остановлена по лимиту выходных "
                    "токенов (finish_reason=length) — ответ может быть "
                    "обрезан. Лимит запроса: %s", payload.get("max_tokens"))
        return data

    async def chat_completion_stream(
        self, messages: list[dict], model: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[dict]:
        client = await self._get_client()
        payload: dict = {
            "model": await self._resolve_model(model), "messages": messages, "stream": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            # локальная генерация бесплатна: поднимаем лимит клиента до минимума
            payload["max_tokens"] = self._effective_max_tokens(
                max_tokens, self.min_max_tokens)
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value

        self._think = _ThinkFilter()
        try:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload
            ) as response:
                if response.status_code != 200:
                    error_bytes = await response.aread()
                    raise OpenRouterError(
                        response.status_code,
                        self._extract_error(error_bytes.decode(errors="replace"))
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        self._strip_delta(choice.get("delta") or {})
                    yield chunk
        except httpx.ConnectError as exc:
            raise OpenRouterError(502, self._unavailable_hint()) from exc

    async def list_models(self) -> list[dict]:
        client = await self._get_client()
        response = await client.get(f"{self.base_url}/models")
        if response.status_code != 200:
            return []
        return response.json().get("data", [])

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class LLMRouter:
    """Маршрутизатор запросов между OpenRouter и локальной моделью.

    Интерфейс совпадает с OpenRouterClient. Активный бэкенд хранится в
    RUNTIME["backend"] (config) и переключается без перезапуска:
    чат-командой, POST /api/backend или заголовком X-LLM-Backend.
    """

    def __init__(self, local_config: Optional[dict] = None):
        self._openrouter = OpenRouterClient()
        self._local = LocalLMClient(local_config or LOCAL_LLM)
        load_runtime_state()

    # ==================== Состояние бэкенда ====================

    @property
    def backend(self) -> str:
        return RUNTIME["backend"]

    def set_backend(self, backend: str) -> str:
        """Переключить активный бэкенд на лету (с сохранением выбора)."""
        if backend not in ("openrouter", "local"):
            raise ValueError(
                f"Неизвестный бэкенд: {backend!r} (openrouter | local)")
        RUNTIME["backend"] = backend
        save_runtime_state()
        logger.info("Активный LLM-бэкенд: %s", backend)
        return backend

    def _resolve(self, model: Optional[str],
                 backend: Optional[str] = None) -> str:
        if backend in ("openrouter", "local"):
            return backend
        if model:
            m = model.strip().lower()
            local_name = (LOCAL_LLM["model"] or "").strip().lower()
            if m.startswith("local/") or (local_name and m == local_name):
                return "local"
            if m.startswith("cloud/"):
                return "openrouter"
        return RUNTIME["backend"]

    async def local_status(self) -> dict:
        """Доступность LM Studio и список загруженных моделей."""
        try:
            models = await self._local.list_models()
            return {"available": True,
                    "models": [m.get("id") for m in models]}
        except Exception as exc:  # noqa: BLE001 — статус не должен падать
            return {"available": False, "error": str(exc)}

    async def backends_available(self) -> dict:
        return {
            "openrouter": {"api_key_set": bool(OPENROUTER["api_key"]),
                           "model": OPENROUTER["model"]},
            "local": {**await self.local_status(),
                      "model": LOCAL_LLM["model"],
                      "base_url": self._local.base_url},
        }

    # ==================== Делегирование ====================

    async def chat_completion(
        self, messages: list[dict], model: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
        backend: Optional[str] = None, **kwargs,
    ) -> dict:
        target = self._resolve(model, backend)
        logger.info("LLM-бэкенд: %s (model=%s)", target, model)
        if target == "local":
            return await self._local.chat_completion(
                messages=messages, model=None, temperature=temperature,
                max_tokens=max_tokens, **kwargs)
        # OpenRouter: модель из .env (model=None), как и раньше
        return await self._openrouter.chat_completion(
            messages=messages, model=None, temperature=temperature,
            max_tokens=max_tokens, **kwargs)

    async def chat_completion_stream(
        self, messages: list[dict], model: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
        backend: Optional[str] = None, **kwargs,
    ) -> AsyncIterator[dict]:
        target = self._resolve(model, backend)
        logger.info("LLM-бэкенд (стрим): %s (model=%s)", target, model)
        if target == "local":
            async for chunk in self._local.chat_completion_stream(
                    messages=messages, model=None, temperature=temperature,
                    max_tokens=max_tokens, **kwargs):
                yield chunk
            return
        async for chunk in self._openrouter.chat_completion_stream(
                messages=messages, model=None, temperature=temperature,
                max_tokens=max_tokens, **kwargs):
            yield chunk

    async def get_models(self) -> list[dict]:
        return await self._openrouter.get_models()

    async def close(self) -> None:
        await self._openrouter.close()
        await self._local.close()
