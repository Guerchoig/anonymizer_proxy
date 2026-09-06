"""
Прокси-сервер для анонимизации запросов к облачным LLM
FastAPI приложение с OpenAI-совместимым API
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Добавляем родительскую директорию в path для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from anonymizer_proxy.config import (
    Mode, PROXY, OPENROUTER, LOGS_DIR, CURRENT_MODE, NER_ENGINE,
    CLOUD_PROVIDER, CLOUD_PROVIDERS, acting_cloud_provider,
    PROXY_VERSION, BASE_DIR, ensure_directories, logger
)
from anonymizer_proxy import launcher
from anonymizer_proxy.anonymizer.ner_service import NERService
from anonymizer_proxy.anonymizer.mapping_store import MappingStore
from anonymizer_proxy.proxy.openrouter_client import OpenRouterError
from anonymizer_proxy.proxy.llm_router import LLMRouter
from anonymizer_proxy.proxy.handlers import RequestHandler
from anonymizer_proxy.env_editor import (
    EnvEditorError, ENV_PATH, apply_updates, read_schema,
)
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

import asyncio
import os
import subprocess


# Глобальные сервисы
ner_service: Optional[NERService] = None
mapping_store: Optional[MappingStore] = None
openrouter_client: Optional[LLMRouter] = None
request_handler: Optional[RequestHandler] = None


def _schedule_proxy_restart(delay: float = 2.0) -> None:
    """Чат-команда «перезапусти прокси»: ответ уже отправлен клиенту —
    через delay запускаем отвязанный хелпер перезапуска (он ждёт
    освобождения порта, поднимает сервер и пишет результат в
    data/logs/restart.log) и завершаем этот процесс.
    """
    async def _job():
        await asyncio.sleep(delay)
        helper = BASE_DIR / "data" / "restart_helper.py"
        if sys.platform == "win32":
            # DETACHED_PROCESS: хелпер живёт после смерти сервера и без окна
            popen_kwargs: dict = {
                "creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        else:
            # macOS/Linux: Windows-флаги не существуют; start_new_session
            # отвязывает процесс от нашей сессии терминала (setsid).
            popen_kwargs = {"start_new_session": True}
        subprocess.Popen(
            [sys.executable, str(helper)],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
        logger.warning("Прокси завершается для перезапуска (чат-команда)")
        await asyncio.sleep(0.2)
        os._exit(0)
    asyncio.get_event_loop().create_task(_job())


def _is_local_bind() -> bool:
    """Проверить, слушает ли сервер только localhost"""
    return PROXY["host"] in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def _acting_provider_key_warning() -> Optional[str]:
    """Предупреждение при старте, если у ДЕЙСТВУЮЩЕГО облачного провайдера
    не задан ключ (чистая функция — тестируется напрямую). None — всё в порядке.
    """
    name = acting_cloud_provider()
    cfg = CLOUD_PROVIDERS.get(name) or {}
    key = cfg.get("api_key") or ""
    if key and "REPLACE_WITH" not in key.upper():
        return None
    key_env = ("OPENROUTER_API_KEY" if name == "openrouter"
               else f"{name.upper()}_API_KEY")
    keys_url = cfg.get("keys_url") or "личный кабинет провайдера"
    return (
        f"Ключ {key_env} не задан (или остался плейсхолдером) — запросы к "
        f"облаку ({name}) будут падать с ошибкой 401. Вставьте ключ "
        f"с {keys_url} в .env или через форму http://127.0.0.1:{PROXY['port']}"
        "/env-editor и перезапустите прокси. Локальная анонимизация работает "
        "и без ключа."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    global ner_service, mapping_store, openrouter_client, request_handler

    logger.info("=" * 60)
    logger.info("  Прокси-сервер анонимизации")
    logger.info("  Версия: %s", PROXY_VERSION)
    logger.info("=" * 60)
    logger.info("  Режим: %s", CURRENT_MODE)
    logger.info("  Интерпретатор: %s", sys.executable)
    logger.info("  NER-движок: %s (device=%s)", NER_ENGINE["model"], NER_ENGINE["device"])

    # Действующий облачный провайдер (последний явно выбранный; сразу после
    # установки — стартовый CLOUD_PROVIDER = openrouter)
    _acting = acting_cloud_provider()
    _cfg = CLOUD_PROVIDERS.get(_acting) or {}
    logger.info("  Облачный провайдер: %s (%s)", _acting, _cfg.get("base_url") or "не настроен")
    logger.info("  Модель: %s", _cfg.get("model") or "не задана")

    _key_warning = _acting_provider_key_warning()
    if _key_warning:
        logger.warning("  [ВНИМАНИЕ] %s", _key_warning)

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

    # Создаём рабочие каталоги (data/, logs/, anonymized_files/, mappings/)
    ensure_directories()

    # Инициализируем сервисы
    ner_service = NERService()
    mapping_store = MappingStore()
    await mapping_store.initialize()

    openrouter_client = LLMRouter()
    request_handler = RequestHandler(
        ner_service=ner_service,
        mapping_store=mapping_store,
        openrouter_client=openrouter_client,
    )

    # Прогреваем локальную NER-модель (загрузка весов при первом запуске)
    try:
        await ner_service.warmup()
        logger.info("  [OK] NER-движок загружен (%s)", ner_service.backend_info())
    except Exception as exc:
        # НЕ понижаем до «будет только regex»: без NER анонимизация файлов и
        # запросов вернёт ошибку NERUnavailableError, а не ограниченный результат.
        # Самая частая причина — сервер запущен глобальным интерпретатором,
        # в котором нет пакета gliner (он установлен только в .venv).
        if sys.platform == "win32":
            interp = ".venv\\Scripts\\python.exe"
            launcher = "start_proxy.cmd"
        else:
            interp, launcher = ".venv/bin/python", "start_proxy.sh"
        logger.error(
            "  [ОШИБКА] NER-движок не загрузился: %s\n"
            "  Анонимизация файлов/запросов будет возвращать ошибку, пока это "
            "не исправлено.\n"
            "  Проверьте путь к интерпретатору выше: если это не %s — "
            "перезапустите прокси скриптом %s из корня проекта или "
            "командой:\n    %s -m anonymizer_proxy.main",
            exc, interp, launcher, interp,
        )

    logger.info("=" * 60)

    yield

    # Очистка
    if request_handler:
        await request_handler.close()


# Создаём FastAPI приложение
app = FastAPI(
    title="Anonymizer Proxy",
    description="Прокси-сервер для анонимизации запросов к облачным LLM",
    version=PROXY_VERSION,
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


async def require_v1_token(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Защита /v1/* эндпоинтов при не-localhost биндинге.

    На localhost доступ открыт (клиент Cline шлёт свой ключ, прокси его
    игнорирует). При прослушивании 0.0.0.0 обязателен PROXY_API_TOKEN —
    иначе любой в сети сможет расходовать кредиты OpenRouter и читать
    локальные файлы через <file_content>-блоки.
    """
    if _is_local_bind():
        return

    configured_token = PROXY["api_token"]
    if not configured_token:
        # lifespan не даст стартовать на не-localhost без токена; это страховка
        raise HTTPException(status_code=401, detail="API token not configured")

    provided = x_api_key
    if authorization:
        auth = authorization
        if auth.lower().startswith("bearer "):
            auth = auth[7:]
        if auth:
            provided = provided or auth

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


# ==================== Keep-alive во время подготовки (NER) ====================
# Раньше прокси не отдавал клиенту ни байта, пока NER + анонимизация не
# завершатся, поэтому Cline мог считать запрос зависшим и таймаутить.
# Теперь подготовка выполняется в фоне, а клиенту периодически отдаются
# нейтральные SSE-комментарии (": keep-alive") — они не попадают в текст
# ответа, но держат HTTP-соединение «живым».

KEEPALIVE_ENABLED = os.getenv("KEEPALIVE_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
KEEPALIVE_INTERVAL = float(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "3"))


async def _stream_with_keepalive(prepare_factory, stream_factory):
    """
    Запустить подготовку запроса (NER + анонимизация) в фоне и параллельно
    отдавать keep-alive SSE-чанки.

    prepare_factory: async-колбэк без аргументов -> подготовленный объект.
    stream_factory: async-колбэк (подготовленный объект) -> AsyncIterator[str]
    (строки SSE-чанков, уже отформатированные).
    """
    prepare_task = asyncio.create_task(prepare_factory())
    try:
        # Первый байт отдаём сразу — клиент не должен упираться в таймаут
        # до первого токена, пока идёт подготовка.
        if KEEPALIVE_ENABLED:
            yield ": keep-alive\n\n"
        while True:
            done, _pending = await asyncio.wait(
                {prepare_task}, timeout=KEEPALIVE_INTERVAL
            )
            if prepare_task in done:
                break
            if KEEPALIVE_ENABLED:
                yield ": keep-alive\n\n"
        prepared = prepare_task.result()  # поднимает исключение при ошибке подготовки
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка подготовки запроса: %s", exc)
        error_chunk = {"error": {"message": str(exc), "type": "server_error"}}
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return
    finally:
        if not prepare_task.done():
            prepare_task.cancel()

    async for event in stream_factory(prepared):
        yield event


@app.post("/v1/chat/completions", dependencies=[Depends(require_v1_token)])
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
            # Чат-команды управления прокси — гибридная детекция (правила +
            # GLiNER, см. RequestHandler.resolve_chat_command). Проверяются
            # ПЕРВЫМИ и в ЛЮБОМ режиме (в т.ч. anonymize=false): команды
            # перехватывает сам прокси, в облако они не уходят никогда.
            command = await request_handler.resolve_chat_command(chat_request)
            if command:
                async def _prepare_command():
                    return await request_handler.execute_chat_command(
                        chat_request, command
                    )

                async def _stream_command(prepared):
                    response, kind = prepared
                    text = (response.choices[0].message.content
                            if response.choices else "") or ""
                    async for event in request_handler.stream_text_response(
                            chat_request, text):
                        yield event
                    if kind == "restart":
                        _schedule_proxy_restart()

                return StreamingResponse(
                    _stream_with_keepalive(_prepare_command, _stream_command),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",  # Для nginx
                    }
                )
            # Passthrough: anonymize=False или режим passthrough по умолчанию
            if not chat_request.anonymize or CURRENT_MODE == Mode.PASSTHROUGH:
                # Автоматическая анонимизация приложенных файлов по явной
                # команде («Анонимизируй файл…») — локальной NER-моделью,
                # без облака. Явное anonymize=false отключает и перехват.
                if chat_request.anonymize and CURRENT_MODE == Mode.PASSTHROUGH:
                    file_paths = request_handler.detect_attached_files_anonymization(
                        chat_request
                    )
                    if file_paths and getattr(
                            openrouter_client, "backend", "openrouter"
                    ) == "openrouter":
                        # Сессию создаём заранее, чтобы проставить X-Session-Id
                        # в заголовке (сама подготовка теперь идёт в фоне).
                        files_session_id = await mapping_store.get_or_create_session(
                            x_session_id
                        )

                        async def _prepare_files():
                            return await request_handler.prepare_files_anonymization(
                                chat_request, file_paths, session_id=files_session_id
                            )

                        async def _stream_files(prepared_files):
                            async for event in request_handler.stream_files_anonymization(
                                chat_request, prepared_files
                            ):
                                yield event

                        return StreamingResponse(
                            _stream_with_keepalive(_prepare_files, _stream_files),
                            media_type="text/event-stream",
                            headers={
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                                "X-Accel-Buffering": "no",  # Для nginx
                                "X-Session-Id": files_session_id,
                            }
                        )
                return StreamingResponse(
                    request_handler.stream_passthrough(chat_request),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",  # Для nginx
                    }
                )

            # NER + анонимизация выполняются в фоне, а клиенту во время
            # подготовки отдаются keep-alive SSE-чанки (см. _stream_with_keepalive).
            async def _prepare():
                return await request_handler.prepare_chat_request(
                    chat_request,
                    session_id=x_session_id
                )

            async def _stream(prepared):
                async for event in request_handler.stream_from_prepared(
                    chat_request,
                    prepared
                ):
                    yield event

            return StreamingResponse(
                _stream_with_keepalive(_prepare, _stream),
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

        # Чат-команда «перезапусти прокси»: ответ уйдёт клиенту, затем
        # процесс завершится и поднимется заново (см. _schedule_proxy_restart)
        meta = getattr(response, "anonymization_metadata", None) or {}
        if meta.get("mode") == "proxy_restart":
            _schedule_proxy_restart()

        # Возвращаем ответ с session_id в заголовке
        return JSONResponse(
            content=response.model_dump(exclude_none=True),
            headers={"X-Session-Id": session_id}
        )

    except json.JSONDecodeError:
        return _make_openai_error(400, "Invalid JSON in request body", "invalid_request_error")
    except OpenRouterError as e:
        return _make_openai_error(e.status_code, str(e), "upstream_error")
    except Exception:
        logger.exception("Необработанная ошибка запроса")
        return _make_openai_error(500, "Internal server error", "server_error")


@app.get("/v1/models", dependencies=[Depends(require_v1_token)])
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
        response = await request_handler.handle_anonymize(request)
        return response
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/deanonymize", dependencies=[Depends(require_api_token)])
async def deanonymize(request: DeanonymizeRequest):
    """Де-анонимизировать текст по session_id"""
    try:
        result = await request_handler.handle_deanonymize(request.text, request.session_id)
        return {"deanonymized_text": result, "session_id": request.session_id}
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions", dependencies=[Depends(require_api_token)])
async def get_sessions():
    """Список активных сессий (с маппингами)."""
    try:
        sessions = await request_handler.handle_get_sessions()
        return {"sessions": sessions, "count": len(sessions)}
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== Сервисные эндпоинты ====================

@app.get("/health")
async def health_check():
    """Проверка работоспособности сервиса"""
    return {
        "status": "healthy",
        "version": PROXY_VERSION,
        "ner_available": ner_service.is_available(),
        "ner_backend": ner_service.backend_info(),
        "mode": CURRENT_MODE,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/backend", dependencies=[Depends(require_api_token)])
async def get_llm_backend():
    """Текущий LLM-бэкенд, ДЕЙСТВУЮЩИЙ облачный провайдер и статус всех"""
    return {
        "backend": openrouter_client.backend,
        # Действующий облачный провайдер (последний явно выбранный; до
        # первого переключения — стартовый CLOUD_PROVIDER = openrouter).
        # "default_cloud_provider" — устаревший алиас для совместимости.
        "acting_cloud_provider": acting_cloud_provider(),
        "default_cloud_provider": acting_cloud_provider(),
        "backends": await openrouter_client.backends_available(),
    }


@app.post("/api/backend", dependencies=[Depends(require_api_token)])
async def set_llm_backend(body: dict):
    """Переключить активный LLM-бэкенд без перезапуска:
    {"backend": "local" | "openrouter" | "gptunnel" | "bothub" | "aitunnel"
    | "genapi" | "custom"}. Облачный провайдер становится действующим
    (запоминается в runtime_state.json)."""
    backend = (body or {}).get("backend")
    try:
        active = openrouter_client.set_backend(backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"backend": active}


@app.get("/api/models", dependencies=[Depends(require_api_token)])
async def get_provider_models(provider: Optional[str] = None):
    """Список моделей провайдера (для выпадающего списка в /env-editor).

    Без параметра — модели действующего облачного провайдера.
    Источник: GET {base_url}/models провайдера (у OpenRouter и GenAPI —
    публичный, у остальных — с ключом); кэш 10 минут."""
    try:
        models = await openrouter_client.get_models(provider)
        return {"object": "list",
                "provider": provider or acting_cloud_provider(),
                "data": models}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as exc:
        logger.warning("Не удалось получить список моделей (%s): %s",
                       provider or "действующий", exc)
        raise HTTPException(status_code=502,
                            detail=f"Список моделей недоступен: {exc}")


# ==================== Редактор настроек (.env из веб-формы) ====================

@app.get("/env-editor", include_in_schema=False)
async def env_editor_page():
    """Экранная форма редактирования .env (все данные — через /api/env/*
    с PROXY_API_TOKEN; страница сама по себе секретов не содержит)."""
    html_path = Path(__file__).parent / "static" / "env_editor.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="env_editor.html не найден")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/env/schema", dependencies=[Depends(require_api_token)])
async def get_env_schema():
    """Белый список редактируемых ключей .env и их состояние.

    Секреты не возвращаются: только признак «задан / не задан».
    """
    try:
        return read_schema()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"{ENV_PATH} не найден")
    except Exception:
        logger.exception("Ошибка чтения схемы .env")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env", dependencies=[Depends(require_api_token)])
async def post_env_updates(body: dict):
    """Обновить ключи .env: {"updates": {KEY: "значение" | null}}.

    Только ключи из белого списка; перед записью создаётся бэкап;
    изменения вступают в силу после перезапуска прокси.
    """
    updates = (body or {}).get("updates") or {}
    try:
        return apply_updates(updates)
    except EnvEditorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Ошибка записи .env")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/restart", dependencies=[Depends(require_api_token)])
async def restart_proxy():
    """Перезапустить прокси (ответ уходит клиенту до завершения процесса).
    Нужен после правки .env: ключи и настройки читаются только при старте."""
    _schedule_proxy_restart(delay=1.5)
    return {
        "message": "Прокси перезапускается. Подождите 20–40 секунд "
                   "(NER-модель загружается заново); готовность — GET /health.",
    }


@app.get("/api/status", dependencies=[Depends(require_api_token)])
async def get_status():
    """Получить статус сервиса"""
    return {
        "version": PROXY_VERSION,
        "llm_backend": openrouter_client.backend,
        "llm_backends": (await openrouter_client.backends_available()) if openrouter_client else {},
        "mode": CURRENT_MODE,
        "ner_engine": {
            "available": ner_service.is_available(),
            "model": NER_ENGINE["model"],
            "backend": ner_service.backend_info(),
            "device": NER_ENGINE["device"],
        },
        "openrouter": {
            "url": OPENROUTER["base_url"],
            "model": OPENROUTER["model"],
            "api_key_set": bool(OPENROUTER["api_key"]),
        },
        "cloud": {
            "acting_provider": acting_cloud_provider(),
            "startup_provider": CLOUD_PROVIDER,
            "providers": {
                name: {
                    "base_url": cfg["base_url"],
                    "model": cfg["model"],
                    "api_key_set": bool(cfg["api_key"]),
                    # VPN-прокси только для openrouter; остальные — напрямую
                    "proxy": cfg["proxy"] or "direct",
                }
                for name, cfg in CLOUD_PROVIDERS.items()
            },
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
    except Exception:
        logger.exception("Внутренняя ошибка сервера")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== Запуск сервера ====================

def _schedule_open_settings(timeout: float = 180.0) -> None:
    """Открыть страницу настроек /env-editor, когда сервер начнёт отвечать
    (запуск ярлыком с флагом --open-settings).

    Отдельный поток-демон опрашивает /health (lifespan-прогрев NER-модели
    может занимать до минуты) и открывает браузер по умолчанию. Поток —
    daemon: остановка сервера его не задерживает. Закрытие открытой страницы
    прокси не останавливает — это только вкладка браузера.
    """
    def _job():
        url = launcher.settings_url()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if launcher.probe_health():
                try:
                    webbrowser.open(url)
                    logger.info("Страница настроек открыта: %s", url)
                except Exception as exc:
                    logger.warning(
                        "Не удалось открыть браузер (%s): %s", url, exc)
                return
            time.sleep(1.0)
        logger.warning(
            "Сервер не ответил за %.0f c — страница настроек не открыта",
            timeout)

    threading.Thread(
        target=_job, daemon=True, name="open-settings").start()


def main(argv=None):
    """Точка входа для запуска сервера"""
    parser = argparse.ArgumentParser(
        prog="anonymizer_proxy.main",
        description="Прокси-сервер анонимизации (OpenAI-совместимый API)")
    parser.add_argument(
        "--open-settings", action="store_true",
        help="открыть страницу настроек /env-editor в браузере, когда сервер "
             "начнёт отвечать (используется ярлыками start_proxy.cmd / "
             "start_proxy.command; без флага — прежнее поведение)")
    args = parser.parse_args(argv)

    host = PROXY["host"]
    port = PROXY["port"]

    logger.info("Запуск сервера на %s:%s...", host, port)
    logger.info("Документация API: http://localhost:%s/docs", port)

    if args.open_settings:
        _schedule_open_settings()

    uvicorn.run(
        "anonymizer_proxy.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()