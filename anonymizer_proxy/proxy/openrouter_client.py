"""
Универсальный клиент облачных LLM-провайдеров с OpenAI-совместимым API.

Один клиент обслуживает OpenRouter, российских провайдеров (GPTunneL,
BotHub, AITUNNEL, GenAPI) и любой пользовательский endpoint
(реестр CLOUD_PROVIDERS в config.py). Провайдеры различаются только
base_url, ключом, схемой авторизации (bearer/plain), заголовками и
наличием VPN-прокси: российские провайдеры ходят напрямую (proxy=None),
VPN-прокси остаётся только у OpenRouter (OPENROUTER_PROXY).
"""
import json
import logging
import os
from typing import AsyncIterator, Optional
import httpx

from ..config import CLOUD_PROVIDERS, OPENROUTER

logger = logging.getLogger("anonymizer_proxy.openrouter")

# Браузерный User-Agent — только для провайдеров с WAF (OpenRouter).
# OpenRouter (WAF перед API) блокирует Python-клиентов по User-Agent:
# 'OpenAI/Python x.y.z', 'Anthropic/Python x.y.z' и 'python-httpx/x.y.z'
# получают HTTP 403 {"success": false, "error": "Access denied by
# security policy."}. Поэтому таким провайдерам шлём браузерный UA.
# Переопределение: CLOUD_USER_AGENT в .env
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")
CLOUD_USER_AGENT = os.getenv("CLOUD_USER_AGENT", _DEFAULT_UA)


class OpenRouterError(Exception):
    """Ошибка API облачного провайдера с HTTP-статусом"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Cloud provider API error ({status_code}): {message}")


def _humanize_error(status_code: int, message: str,
                    provider: str = "openrouter") -> str:
    """Дописать к сырой ошибке провайдера понятное объяснение и что делать.

    Прокси ретранслирует ошибки облака в чат модели/пользователя — без
    пояснения сырой текст вроде «User not found.» не подсказывает, что
    ключ API на этой машине не работает.
    """
    low = (message or "").lower()
    if status_code == 401:
        if provider == "openrouter" and "user not found" in low:
            return (
                f"{message}. OpenRouter не признал ключ API: проверьте в .env "
                "OPENROUTER_API_KEY — должен начинаться с 'sk-or-v1-', быть без "
                "кавычек и пробелов и быть действительным (создаётся и "
                "проверяется на openrouter.ai/keys). Ключ подхватывается только "
                "при старте прокси — после правки .env перезапустите сервер."
            )
        return (
            f"{message}. Провайдер {provider} не принял ключ API: проверьте в "
            f".env переменную {provider.upper()}_API_KEY (без кавычек и "
            "пробелов, действительный ключ из личного кабинета провайдера). "
            "Ключ подхватывается только при старте прокси — после правки "
            ".env перезапустите сервер."
        )
    if status_code == 402:
        return (
            f"{message}. Недостаточно кредитов у провайдера {provider} для "
            "этой модели — пополните баланс или выберите бесплатную модель."
        )
    if status_code == 429:
        return (
            f"{message}. Лимит запросов у провайдера {provider} исчерпан — "
            "повторите позже, смените модель или провайдера."
        )
    return message


class OpenAICompatClient:
    """HTTP клиент OpenAI-совместимого облачного провайдера"""

    def __init__(self, config: Optional[dict] = None,
                 api_key: Optional[str] = None):
        cfg = config or OPENROUTER
        # Реестровая запись хранится по ссылке: правки .env из формы
        # (env_editor._sync_registry) видны клиенту БЕЗ перезапуска
        self._cfg = cfg
        self._api_key_override = api_key or ""
        self.provider = cfg.get("name", "custom")
        self._client: Optional[httpx.AsyncClient] = None

    # Значения читаются динамически из реестра — правки .env применяются
    # к работающему серверу без пересоздания клиента
    @property
    def api_key(self) -> str:
        return self._api_key_override or self._cfg.get("api_key") or ""

    @property
    def model(self) -> str:
        return self._cfg.get("model") or ""

    @property
    def base_url(self) -> str:
        return (self._cfg.get("base_url") or "").rstrip("/")

    @property
    def timeout(self) -> float:
        return float(self._cfg.get("timeout", 120.0))

    @property
    def proxy(self) -> Optional[str]:
        # VPN-прокси только там, где он задан для ЭТОГО провайдера
        # (openrouter — OPENROUTER_PROXY; российские — None, прямой доступ)
        return self._cfg.get("proxy") or None

    @property
    def auth_scheme(self) -> str:
        # bearer — "Authorization: Bearer <key>"; plain — ключ без префикса
        return self._cfg.get("auth_scheme", "bearer")

    @property
    def browser_ua(self) -> bool:
        return bool(self._cfg.get("browser_ua"))

    @property
    def extra_headers(self) -> dict:
        return dict(self._cfg.get("extra_headers") or {})

    def _auth_headers(self) -> dict:
        """Заголовки авторизации и служебные заголовки провайдера."""
        headers = {"Content-Type": "application/json"}
        if self.auth_scheme == "plain":
            # GPTunneL: ключ в Authorization без префикса Bearer
            headers["Authorization"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.browser_ua:
            headers["User-Agent"] = CLOUD_USER_AGENT
        headers.update(self.extra_headers)
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент с заголовками и прокси ЭТОГО провайдера"""
        if self._client is None or self._client.is_closed:
            client_kwargs: dict = {
                "timeout": self.timeout,
                "headers": self._auth_headers(),
            }
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
                logger.info("Провайдер %s: запросы через прокси %s",
                            self.provider, self.proxy)
            else:
                logger.info("Провайдер %s: прямой доступ (без VPN-прокси)",
                            self.provider)
            self._client = httpx.AsyncClient(**client_kwargs)
        return self._client

    def _resolve_model(self, model: Optional[str]) -> str:
        """Модель для запроса: явная (после среза префикса провайдера) > из .env"""
        actual_model = model or self.model
        if self.model and actual_model != self.model:
            logger.info("Провайдер %s: модель из запроса '%s' (из .env: '%s')",
                        self.provider, actual_model, self.model)
        return actual_model

    async def chat_completion(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> dict:
        """
        Отправить запрос на chat completion

        Args:
            messages: Список сообщений
            model: Модель (по умолчанию из конфига)
            temperature: Температура генерации
            max_tokens: Максимальное количество токенов
            **kwargs: Дополнительные параметры

        Returns:
            Ответ от API в формате OpenAI

        Raises:
            OpenRouterError: при ошибке API
        """
        client = await self._get_client()

        actual_model = self._resolve_model(model)
        payload = {
            "model": actual_model,
            "messages": messages,
        }
        logger.info("Отправка запроса (%s): model=%s", self.provider,
                    actual_model)

        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # Добавляем дополнительные параметры
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value

        response = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error("Ошибка %s: %s", response.status_code, error_text[:500])
            try:
                error_data = response.json()
                error_text = error_data.get("error", {}).get("message", error_text)
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
            raise OpenRouterError(
                response.status_code,
                _humanize_error(response.status_code, error_text,
                                self.provider)
            )

        return response.json()

    async def chat_completion_stream(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncIterator[dict]:
        """
        Отправить запрос на chat completion со стримингом

        Yields:
            Чанки ответа в формате SSE

        Raises:
            OpenRouterError: при ошибке API/таймауте/соединении
        """
        client = await self._get_client()

        actual_model = self._resolve_model(model)
        payload = {
            "model": actual_model,
            "messages": messages,
            "stream": True,
        }
        logger.info("Стриминг запрос (%s): model=%s", self.provider,
                    actual_model)
        logger.debug("URL: %s/chat/completions", self.base_url)

        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value

        logger.info("Открываем стриминг соединение...")

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload
            ) as response:
                logger.info("Статус ответа: %s", response.status_code)

                if response.status_code != 200:
                    error_bytes = await response.aread()
                    error_text = error_bytes.decode(errors="replace")
                    logger.error("Ошибка: %s", error_text[:500])
                    try:
                        error_text = json.loads(error_text).get(
                            "error", {}).get("message", error_text)
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass
                    raise OpenRouterError(
                        response.status_code,
                        _humanize_error(response.status_code, error_text,
                                        self.provider)
                    )

                logger.info("Начинаем чтение chunk'ов...")
                chunk_count = 0
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]  # Убираем "data: "
                        if data == "[DONE]":
                            logger.info("Стриминг завершён. Всего chunk'ов: %d", chunk_count)
                            break
                        try:
                            chunk = json.loads(data)
                            chunk_count += 1
                            # Логируем модель из chunk'а (только первый и каждый 10-й)
                            if chunk_count == 1 or chunk_count % 10 == 0:
                                chunk_model = chunk.get("model", "не указана")
                                logger.debug("Chunk #%d: model=%s", chunk_count, chunk_model)
                            yield chunk
                        except json.JSONDecodeError:
                            continue
        except httpx.TimeoutException as e:
            logger.error("ТАЙМАУТ: %s", e)
            raise OpenRouterError(504, f"{self.provider} timeout: {e}")
        except httpx.ConnectError as e:
            logger.error("ОШИБКА СОЕДИНЕНИЯ: %s", e)
            raise OpenRouterError(
                502, f"{self.provider} connection error: {e}")

    async def get_models(self) -> list[dict]:
        """Получить список доступных моделей"""
        client = await self._get_client()
        response = await client.get(f"{self.base_url}/models")

        if response.status_code != 200:
            return []

        data = response.json()
        return data.get("data", [])

    async def close(self):
        """Закрыть HTTP клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class OpenRouterClient(OpenAICompatClient):
    """Клиент дефолтного облачного провайдера (обратная совместимость).

    Ранее обслуживал только OpenRouter; теперь это клиент провайдера,
    выбранного CLOUD_PROVIDER (по умолчанию — openrouter).
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(config=OPENROUTER, api_key=api_key)