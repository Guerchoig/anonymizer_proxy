"""
Pydantic схемы данных для прокси-сервера анонимизации
"""
import re
from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator

from ..config import CURRENT_MODE

# Допустимый формат session_id (защита от path traversal и инъекций)
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_session_id(session_id: Optional[str]) -> Optional[str]:
    """Проверить формат session_id (None допускается)"""
    if session_id is not None and not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(
            "session_id должен содержать только буквы, цифры, '_' и '-' (1-64 символа)"
        )
    return session_id


class Entity(BaseModel):
    """Обнаруженная сущность в тексте"""
    text: str = Field(..., description="Текст сущности")
    type: str = Field(..., description="Тип сущности (PERSON, ORG, etc.)")
    start: int = Field(..., description="Начальная позиция в тексте")
    end: int = Field(..., description="Конечная позиция в тексте")
    confidence: float = Field(default=1.0, description="Уверенность детекции")


class MappingEntry(BaseModel):
    """Запись маппинга токен ↔ оригинальное значение"""
    token: str = Field(..., description="Токен замены, например [PERSON_1]")
    original_value: str = Field(..., description="Оригинальное значение")
    entity_type: str = Field(..., description="Тип сущности")
    session_id: str = Field(..., description="ID сессии")
    created_at: datetime = Field(default_factory=datetime.now)


class AnonymizationResult(BaseModel):
    """Результат анонимизации текста"""
    original_text: str = Field(..., description="Оригинальный текст")
    anonymized_text: str = Field(..., description="Анонимизированный текст")
    entities_found: list[Entity] = Field(default_factory=list)
    mappings: list[MappingEntry] = Field(default_factory=list)
    processing_time_ms: float = Field(default=0.0)


class ChatMessage(BaseModel):
    """Сообщение в чате (OpenAI формат)"""
    role: Literal["system", "user", "assistant", "tool"] = Field(...)
    # content может отсутствовать у assistant-сообщений с tool_calls
    content: str | list[dict] | None = Field(default=None)
    name: Optional[str] = None
    # Поля tool-calling (агенты типа Cline)
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """Запрос к Chat Completion API (OpenAI совместимый)"""
    model: str = Field(...)
    messages: list[ChatMessage] = Field(...)
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[str | list[str]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None
    # Tool calling: определения инструментов агента (например, Cline).
    # Обязательно пробрасывать в облако, иначе модель не сможет вызывать
    # инструменты.
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = None
    stream_options: Optional[dict] = None
    # Дополнительные поля для управления анонимизацией
    anonymize: bool = Field(default=True, description="Включить анонимизацию")
    mode: Literal["full", "anonymize_only", "review"] = Field(
        default=CURRENT_MODE, 
        description="Режим: full - полная обработка, anonymize_only - только анонимизация, review - ручное ревью перед отправкой (по умолчанию из .env)"
    )


class ChatCompletionChoice(BaseModel):
    """Вариант ответа в Chat Completion"""
    index: int = Field(...)
    message: ChatMessage = Field(...)
    finish_reason: Optional[str] = None


class UsageInfo(BaseModel):
    """Информация об использовании токенов"""
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    total_tokens: int = Field(default=0)


class ChatCompletionResponse(BaseModel):
    """Ответ от Chat Completion API"""
    id: str = Field(...)
    object: str = Field(default="chat.completion")
    created: int = Field(...)
    model: str = Field(...)
    choices: list[ChatCompletionChoice] = Field(...)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    # Метаданные анонимизации
    anonymization_metadata: Optional[dict] = None


class AnonymizeRequest(BaseModel):
    """Запрос на анонимизацию (режим anonymize_only)"""
    text: Optional[str] = Field(None, description="Текст для анонимизации")
    files: Optional[list[dict]] = Field(None, description="Файлы для анонимизации")
    session_id: Optional[str] = Field(None, description="ID сессии для переиспользования маппингов")

    @field_validator("session_id")
    @classmethod
    def _check_session_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_session_id(v)


class DeanonymizeRequest(BaseModel):
    """Запрос на де-анонимизацию текста"""
    text: str = Field(..., description="Текст с токенами для восстановления")
    session_id: str = Field(..., description="ID сессии с маппингами")

    @field_validator("session_id")
    @classmethod
    def _check_session_id(cls, v: str) -> str:
        result = validate_session_id(v)
        if result is None:
            raise ValueError("session_id обязателен")
        return result


class AnonymizeResponse(BaseModel):
    """Ответ с анонимизированными данными"""
    session_id: str = Field(...)
    anonymized_text: Optional[str] = Field(None)
    anonymized_files: Optional[list[dict]] = Field(None)
    entities_found: list[Entity] = Field(default_factory=list)
    mappings_count: int = Field(default=0)
    processing_time_ms: float = Field(default=0.0)
    storage_path: Optional[str] = Field(None, description="Путь к сохранённым файлам")


class LogEntry(BaseModel):
    """Запись лога"""
    timestamp: datetime = Field(default_factory=datetime.now)
    session_id: str = Field(...)
    request_type: str = Field(...)  # "chat_completion", "anonymize_only"
    original_content: Any = Field(...)
    anonymized_content: Any = Field(...)
    response_content: Optional[Any] = None
    entities_found: list[Entity] = Field(default_factory=list)
    processing_time_ms: float = Field(default=0.0)
    error: Optional[str] = None


class FileInfo(BaseModel):
    """Информация о файле"""
    filename: str = Field(...)
    content_type: str = Field(...)
    size_bytes: int = Field(...)
    original_path: Optional[str] = None
    anonymized_path: Optional[str] = None


class SessionInfo(BaseModel):
    """Информация о сессии"""
    session_id: str = Field(...)
    created_at: datetime = Field(...)
    expires_at: datetime = Field(...)
    mappings_count: int = Field(default=0)
    files_processed: list[FileInfo] = Field(default_factory=list)


class ReviewApproveRequest(BaseModel):
    """Запрос на одобрение запроса из очереди ревью"""
    request_id: str = Field(..., description="ID запроса из очереди ревью")
    edited_content: Optional[str] = Field(
        None,
        description="Отредактированный контент (если None — использовать оригинальный .md)"
    )


class ReviewRejectRequest(BaseModel):
    """Запрос на отклонение запроса из очереди ревью"""
    request_id: str = Field(..., description="ID запроса из очереди ревью")
    reason: Optional[str] = Field(None, description="Причина отклонения")


class PendingReviewItem(BaseModel):
    """Элемент списка запросов, ожидающих ревью"""
    request_id: str = Field(...)
    session_id: str = Field(...)
    anonymized_file_path: str = Field(...)
    created_at: str = Field(...)


class PendingReviewsResponse(BaseModel):
    """Ответ со списком запросов, ожидающих ревью"""
    pending: list[PendingReviewItem] = Field(default_factory=list)
    count: int = Field(default=0)