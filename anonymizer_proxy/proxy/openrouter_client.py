"""
Клиент для работы с OpenRouter API
"""
import json
import logging
import os
from typing import AsyncIterator, Optional
import httpx

from ..config import OPENROUTER

logger = logging.getLogger("anonymizer_proxy.openrouter")

# Прокси для доступа к OpenRouter (Happ VPN)
# Если нужно использовать VPN-прокси, укажите в .env:
# OPENROUTER_PROXY=http://127.0.0.1:10809  (HTTP)
# или OPENROUTER_PROXY=socks5://127.0.0.1:10808  (SOCKS5)
OPENROUTER_PROXY = os.getenv("OPENROUTER_PROXY", None)

# User-Agent для запросов к OpenRouter.
# OpenRouter (WAF перед API) блокирует Python-клиентов по User-Agent:
# 'OpenAI/Python x.y.z', 'Anthropic/Python x.y.z' и 'python-httpx/x.y.z'
# получают HTTP 403 {"success": false, "error": "Access denied by
# security policy."}. Поэтому по умолчанию шлём браузерный UA — его WAF
# пропускает. При необходимости переопределите в .env:
# OPENROUTER_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64)
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")
OPENROUTER_USER_AGENT = os.getenv("OPENROUTER_USER_AGENT", _DEFAULT_UA)


class OpenRouterError(Exception):
    """Ошибка OpenRouter API с HTTP-статусом"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"OpenRouter API error ({status_code}): {message}")


def _humanize_error(status_code: int, message: str) -> str:
    """Дописать к сырой ошибке OpenRouter понятное объяснение и что делать.

    Прокси ретранслирует ошибки облака в чат модели/пользователя — без
    пояснения сырой текст вроде «User not found.» не подсказывает, что
    ключ API на этой машине не работает.
    """
    low = (message or "").lower()
    if status_code == 401 and "user not found" in low:
        return (
            f"{message}. OpenRouter не признал ключ API: проверьте в .env "
            "OPENROUTER_API_KEY — должен начинаться с 'sk-or-v1-', быть без "
            "кавычек и пробелов и быть действительным (создаётся и "
            "проверяется на openrouter.ai/keys). Ключ подхватывается только "
            "при старте прокси — после правки .env перезапустите сервер."
        )
    if status_code == 402:
        return (
            f"{message}. Недостаточно кредитов OpenRouter для этой модели — "
            "пополните баланс или выберите бесплатную модель."
        )
    if status_code == 429:
        return (
            f"{message}. Лимит запросов OpenRouter исчерпан — повторите "
            "позже или смените модель."
        )
    return message


class OpenRouterClient:
    """HTTP клиент для OpenRouter API"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or OPENROUTER["api_key"]
        self.base_url = OPENROUTER["base_url"]
        self.timeout = OPENROUTER["timeout"]
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент с нужными заголовками и прокси для OpenRouter"""
        if self._client is None or self._client.is_closed:
            client_kwargs = {
                "timeout": self.timeout,
                "headers": {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost:8081",  # Для OpenRouter
                    "X-Title": "Anonymizer Proxy",
                    # Без браузерного UA OpenRouter отвечает 403
                    # "Access denied by security policy." (см. комментарий выше)
                    "User-Agent": OPENROUTER_USER_AGENT,
                }
            }

            # Если указан прокси для OpenRouter (VPN) — используем его
            if OPENROUTER_PROXY:
                client_kwargs["proxy"] = OPENROUTER_PROXY
                logger.info("Используется прокси: %s", OPENROUTER_PROXY)

            self._client = httpx.AsyncClient(**client_kwargs)
        return self._client

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

        # ВСЕГДА используем модель из конфига, игнорируем модель из запроса
        actual_model = OPENROUTER["model"]
        if model is not None and model != actual_model:
            logger.warning(
                "Запрошена модель '%s', но используется модель из .env: '%s'",
                model, actual_model
            )

        payload = {
            "model": actual_model,
            "messages": messages,
        }
        logger.info("Отправка запроса: model=%s (из .env)", actual_model)

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
                _humanize_error(response.status_code, error_text)
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

        # ВСЕГДА используем модель из конфига, игнорируем модель из запроса
        actual_model = OPENROUTER["model"]
        if model is not None and model != actual_model:
            logger.warning(
                "Запрошена модель '%s', но используется модель из .env: '%s'",
                model, actual_model
            )

        payload = {
            "model": actual_model,
            "messages": messages,
            "stream": True,
        }
        logger.info("Стриминг запрос: model=%s (из .env)", actual_model)
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
                        _humanize_error(response.status_code, error_text)
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
            raise OpenRouterError(504, f"OpenRouter timeout: {e}")
        except httpx.ConnectError as e:
            logger.error("ОШИБКА СОЕДИНЕНИЯ: %s", e)
            raise OpenRouterError(502, f"OpenRouter connection error: {e}")

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