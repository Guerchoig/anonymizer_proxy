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
    mode: Literal["full", "manual"] = Field(
        default=CURRENT_MODE, 
        description="Режим: full - полная обработка (анонимизация → облако → де-анонимизация), manual - без анонимизации (явное управление)"
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
    """Запрос на анонимизацию текста/файлов без отправки в облако (ручной сценарий)"""
    text: Optional[str] = Field(None, description="Текст для анонимизации")
    files: Optional[list[dict]] = Field(None, description="Файлы для анонимизации")
    session_id: Optional[str] = Field(None, description="ID сессии для переиспользования маппингов")

    @field_validator("session_id")
    @classmethod
    def _check_session_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_session_id(v)


class AnonymizeFileRequest(BaseModel):
    """Запрос на анонимизацию локального файла (создание копии рядом с оригиналом)"""
    file_path: str = Field(..., description="Путь к локальному файлу")
    session_id: Optional[str] = Field(None, description="ID сессии для маппингов")
    output_path: Optional[str] = Field(
        None,
        description="Куда сохранить анонимизированную копию (по умолчанию — <name>.anonymized.<ext> рядом с оригиналом)",
    )


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


class SendAnonymizedRequest(BaseModel):
    """Запрос на отправку анонимизированного промпта в облако"""
    session_id: str = Field(..., description="ID сессии с маппингами")
    content: str = Field(..., description="Анонимизированный контент (markdown или текст)")
    stream: Optional[bool] = Field(False, description="Стриминг ответа")
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    @field_validator("session_id")
    @classmethod
    def _check_session_id(cls, v: str) -> str:
        result = validate_session_id(v)
        if result is None:
            raise ValueError("session_id обязателен")
        return result


class DeanonymizeFileRequest(BaseModel):
    """Запрос на де-анонимизацию файла (замена плейсхолдеров на реальные значения)"""
    session_id: str = Field(..., description="ID сессии с маппингами")
    file_path: str = Field(..., description="Путь к файлу с плейсхолдерами")
    output_path: Optional[str] = Field(
        None,
        description="Куда сохранить результат (по умолчанию — перезапись исходного)",
    )

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
    request_type: str = Field(...)  # "chat_completion", "files_*", "anonymize"
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
    review_files: list[str] = Field(default_factory=list)  # пути к .md файлам ревью


class SessionsResponse(BaseModel):
    """Ответ со списком активных сессий"""
    sessions: list[SessionInfo] = Field(default_factory=list)
    count: int = Field(default=0)