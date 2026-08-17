"""
Прокси-сервер для анонимизации запросов к облачным LLM
FastAPI приложение с OpenAI-совместимым API
"""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Добавляем родительскую директорию в path для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from anonymizer_proxy.config import (
    Mode, PROXY, OPENROUTER, LOGS_DIR, CURRENT_MODE, logger
)
from anonymizer_proxy.anonymizer.ner_service import NERService
from anonymizer_proxy.anonymizer.mapping_store import MappingStore
from anonymizer_proxy.proxy.openrouter_client import OpenRouterClient, OpenRouterError
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.models.schemas import (
    ChatCompletionRequest,
    AnonymizeRequest,
    AnonymizeResponse,
    AnonymizeFileRequest,
    DeanonymizeRequest,
    DeanonymizeFileRequest,
    SendAnonymizedRequest,
    SessionsResponse,
    validate_session_id,
)


# Глобальные сервисы
ner_service: Optional[NERService] = None
mapping_store: Optional[MappingStore] = None
openrouter_client: Optional[OpenRouterClient] = None
request_handler: Optional[RequestHandler] = None


def _is_local_bind() -> bool:
    """Проверить, слушает ли сервер только localhost"""
    return PROXY["host"] in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    global ner_service, mapping_store, openrouter_client, request_handler

    logger.info("=" * 60)
    logger.info("  Прокси-сервер анонимизации")
    logger.info("=" * 60)
    logger.info("  Режим: %s", CURRENT_MODE)
    logger.info("  LM Studio: %s", os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"))
    logger.info("  OpenRouter: %s", OPENROUTER["base_url"])
    logger.info("  Модель: %s", OPENROUTER["model"])
    logger.info("  Логи: %s", LOGS_DIR)
    logger.info("=" * 60)

    # Защита: при прослушивании не-localhost обязателен API-токен
    if not _is_local_bind() and not PROXY["api_token"]:
        raise RuntimeError(
            "PROXY_API_TOKEN не задан, а сервер слушает не-localhost "
            f"({PROXY['host']}). Задайте PROXY_API_TOKEN в .env или используйте "
            "PROXY_HOST=127.0.0.1"
        )
    if not PROXY["api_token"]:
        logger.warning(
            "PROXY_API_TOKEN не задан — /api/* эндпоинты доступны без токена "
            "(допустимо только для localhost)"
        )

    # Инициализируем сервисы
    ner_service = NERService()
    mapping_store = MappingStore()
    await mapping_store.initialize()

    openrouter_client = OpenRouterClient()
    request_handler = RequestHandler(
        ner_service=ner_service,
        mapping_store=mapping_store,
        openrouter_client=openrouter_client,
    )

    # Проверяем доступность LM Studio
    lm_available = await ner_service.check_lm_studio()
    if lm_available:
        logger.info("  [OK] LM Studio доступен")
    else:
        logger.warning("  [FAIL] LM Studio недоступен (будет использоваться только regex)")

    logger.info("=" * 60)

    yield

    # Очистка
    if request_handler:
        await request_handler.close()


# Создаём FastAPI приложение
app = FastAPI(
    title="Anonymizer Proxy",
    description="Прокси-сервер для анонимизации запросов к облачным LLM",
    version="1.1.0",
    lifespan=lifespan,
)

# CORS middleware: ограничиваем локальными origin, credentials не передаём
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Session-Id"],
)


# ==================== Аутентификация для /api/* ====================

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_api_token(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Проверка токена для административных /api/* эндпоинтов.
    Токен передаётся как 'Authorization: Bearer <token>' или 'X-API-Key: <token>'.
    Если PROXY_API_TOKEN не задан и сервер на localhost — доступ разрешён.
    """
    configured_token = PROXY["api_token"]
    if not configured_token:
        if _is_local_bind():
            return
        raise HTTPException(status_code=401, detail="API token not configured")

    provided = x_api_key
    if authorization is not None:
        provided = authorization.credentials

    if not provided or provided != configured_token:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


# ==================== OpenAI-совместимые эндпоинты ====================

def _make_openai_error(status_code: int, message: str, error_type: str = "server_error") -> JSONResponse:
    """Создать OpenAI-совместимый error response"""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": error_type,
            }
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None, alias="X-Session-Id"),
):
    """
    OpenAI-совместимый эндпоинт для chat completions
    Анонимизирует запрос, отправляет в OpenRouter, де-анонимизирует ответ
    Поддерживает как обычный, так и стриминг режим
    """
    try:
        # Валидация session_id из заголовка (защита от мусора/инъекций)
        try:
            x_session_id = validate_session_id(x_session_id)
        except ValueError as ve:
            return _make_openai_error(400, str(ve), "invalid_request_error")

        body = await request.json()
        logger.info("Получен запрос: model=%s, stream=%s", body.get("model"), body.get("stream"))

        # Парсим запрос
        chat_request = ChatCompletionRequest(**body)
        logger.info("Режим: %s", chat_request.mode)

        # Стриминг режим
        if chat_request.stream:
            # Passthrough: anonymize=False или режим passthrough по умолчанию
            if not chat_request.anonymize or CURRENT_MODE == Mode.PASSTHROUGH:
                return StreamingResponse(
                    request_handler.stream_passthrough(chat_request),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",  # Для nginx
                    }
                )

            # Сначала выполняем NER + анонимизацию ДО создания StreamingResponse
            # Это позволяет вернуть нормальный JSON error при ошибке
            try:
                prepared = await request_handler.prepare_chat_request(
                    chat_request,
                    session_id=x_session_id
                )
            except Exception as prep_error:
                error_msg = str(prep_error)
                logger.error("Ошибка подготовки запроса: %s", error_msg)
                return _make_openai_error(500, error_msg, "server_error")

            return StreamingResponse(
                request_handler.stream_from_prepared(
                    chat_request,
                    prepared
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # Для nginx
                }
            )

        # Обычный режим (без стриминга)
        response, session_id = await request_handler.handle_chat_completion(
            chat_request,
            session_id=x_session_id
        )

        # Возвращаем ответ с session_id в заголовке
        return JSONResponse(
            content=response.model_dump(exclude_none=True),
            headers={"X-Session-Id": session_id}
        )

    except json.JSONDecodeError:
        return _make_openai_error(400, "Invalid JSON in request body", "invalid_request_error")
    except OpenRouterError as e:
        return _make_openai_error(e.status_code, str(e), "upstream_error")
    except Exception as e:
        logger.exception("Необработанная ошибка запроса")
        return _make_openai_error(500, str(e), "server_error")


@app.get("/v1/models")
async def list_models():
    """Список доступных моделей (проксирует от OpenRouter)"""
    try:
        models = await openrouter_client.get_models()
        return {"object": "list", "data": models}
    except Exception:
        # Возвращаем хотя бы нашу модель
        return {
            "object": "list",
            "data": [
                {
                    "id": OPENROUTER["model"],
                    "object": "model",
                    "created": int(datetime.now().timestamp()),
                    "owned_by": "openrouter",
                }
            ]
        }


# ==================== Эндпоинты для анонимизации ====================

@app.post("/api/anonymize", response_model=AnonymizeResponse, dependencies=[Depends(require_api_token)])
async def anonymize(request: AnonymizeRequest):
    """
    Эндпоинт для анонимизации без отправки в облако
    Поддерживает текст и файлы (base64)
    """
    try:
        response = await request_handler.handle_anonymize_only(request)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/anonymize_file", dependencies=[Depends(require_api_token)])
async def anonymize_file(request: AnonymizeFileRequest):
    """Анонимизировать локальный файл (создать копию с плейсхолдерами рядом с оригиналом)"""
    try:
        return await request_handler.handle_anonymize_file(
            request.file_path, request.session_id, request.output_path
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/deanonymize", dependencies=[Depends(require_api_token)])
async def deanonymize(request: DeanonymizeRequest):
    """Де-анонимизировать текст по session_id"""
    try:
        result = await request_handler.handle_deanonymize(request.text, request.session_id)
        return {"deanonymized_text": result, "session_id": request.session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/deanonymize_file", dependencies=[Depends(require_api_token)])
async def deanonymize_file(request: DeanonymizeFileRequest):
    """Де-анонимизировать файл (заменить плейсхолдеры на реальные значения)"""
    try:
        result = await request_handler.handle_deanonymize_file(
            request.session_id, request.file_path, request.output_path
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/send", dependencies=[Depends(require_api_token)])
async def send_anonymized(request: SendAnonymizedRequest):
    """Отправить анонимизированный промпт в облако и де-анонимизировать ответ"""
    if request.stream:
        return StreamingResponse(
            request_handler.stream_send_anonymized(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Для nginx
            }
        )
    try:
        response, session_id = await request_handler.handle_send_anonymized(request)
        return JSONResponse(
            content=response.model_dump(exclude_none=True),
            headers={"X-Session-Id": session_id}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Эндпоинты для логирования ====================

@app.get("/api/logs", dependencies=[Depends(require_api_token)])
async def get_logs(session_id: Optional[str] = None, limit: int = 100):
    """Получить логи (все или по session_id)"""
    try:
        session_id = validate_session_id(session_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    try:
        if session_id:
            logs = await request_handler.get_session_logs(session_id, limit)
        else:
            logs = await request_handler.get_all_logs(limit)
        return {"logs": logs, "count": len(logs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/export", dependencies=[Depends(require_api_token)])
async def export_logs(session_id: Optional[str] = None):
    """Экспортировать логи в JSON файл"""
    try:
        session_id = validate_session_id(session_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    try:
        if session_id:
            logs = await request_handler.get_session_logs(session_id, limit=10000)
        else:
            logs = await request_handler.get_all_logs(limit=10000)

        # Сохраняем в файл (session_id уже провалидирован — path traversal невозможен)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"logs_{session_id or 'all'}_{timestamp}.json"
        filepath = LOGS_DIR / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2, default=str)

        return {
            "message": "Логи экспортированы",
            "filepath": str(filepath),
            "count": len(logs)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions", dependencies=[Depends(require_api_token)])
async def get_sessions():
    """Список активных сессий (с маппингами)."""
    try:
        sessions = await request_handler.handle_get_sessions()
        return {"sessions": sessions, "count": len(sessions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Сервисные эндпоинты ====================

@app.get("/health")
async def health_check():
    """Проверка работоспособности сервиса"""
    lm_available = await ner_service.check_lm_studio()

    return {
        "status": "healthy",
        "lm_studio_available": lm_available,
        "mode": CURRENT_MODE,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/status", dependencies=[Depends(require_api_token)])
async def get_status():
    """Получить статус сервиса"""
    lm_available = await ner_service.check_lm_studio()

    return {
        "mode": CURRENT_MODE,
        "lm_studio": {
            "available": lm_available,
            "url": os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
        },
        "openrouter": {
            "url": OPENROUTER["base_url"],
            "model": OPENROUTER["model"],
            "api_key_set": bool(OPENROUTER["api_key"]),
        },
        "storage": {
            "logs_dir": str(LOGS_DIR),
        },
    }


@app.post("/api/cleanup", dependencies=[Depends(require_api_token)])
async def cleanup_expired():
    """Очистить истёкшие сессии"""
    try:
        await mapping_store.cleanup_expired()
        return {"message": "Истёкшие сессии очищены"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Запуск сервера ====================

def main():
    """Точка входа для запуска сервера"""
    host = PROXY["host"]
    port = PROXY["port"]

    logger.info("Запуск сервера на %s:%s...", host, port)
    logger.info("Документация API: http://localhost:%s/docs", port)

    uvicorn.run(
        "anonymizer_proxy.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()