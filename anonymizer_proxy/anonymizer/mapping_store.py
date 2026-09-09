"""
Хранилище маппингов токенов и оригинальных значений
Использует SQLite (WAL) для персистентности и in-memory cache для быстрого доступа
"""
import asyncio
import json
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence
import aiosqlite

from ..config import DB_PATH, MAPPING_TTL_SECONDS, MAPPINGS_DIR, ANONYMIZED_FILES_DIR
from ..models.schemas import Entity, MappingEntry

logger = logging.getLogger("anonymizer_proxy.store")


def _utcnow() -> datetime:
    """Единое UTC-время (согласовано с CURRENT_TIMESTAMP в SQLite)"""
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    """ISO-строка в UTC для сравнения с CURRENT_TIMESTAMP"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def _from_iso(value: str) -> datetime:
    """Парсинг ISO-строки из БД (UTC, naive)"""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return _utcnow().replace(tzinfo=None)


def _token_counter(token: str) -> int:
    """Числовой суффикс токена вида '[TYPE_12]' -> 12 (0, если не распознан)."""
    try:
        return int(token.rstrip("]").rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return 0


class MappingStore:
    """Хранилище маппингов для анонимизации/де-анонимизации"""

    def __init__(self):
        self._db_path = DB_PATH
        self._memory_cache: dict[str, dict[str, MappingEntry]] = {}  # session_id -> {token -> entry}
        self._initialized = False
        self._db: Optional[aiosqlite.Connection] = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def _get_db(self) -> aiosqlite.Connection:
        """
        Одно разделяемое соединение (вместо нового на каждую операцию).
        WAL-режим позволяет параллельные чтения при записи.
        """
        if self._db is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
        return self._db

    async def initialize(self):
        """Инициализировать базу данных"""
        if self._initialized:
            return

        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                token TEXT NOT NULL,
                original_value TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, token)
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_token 
            ON mappings(session_id, token)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                metadata TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                request_type TEXT NOT NULL,
                original_content TEXT,
                anonymized_content TEXT,
                response_content TEXT,
                entities_found TEXT,
                processing_time_ms REAL,
                error TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS file_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(file_path, session_id)
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_file_sessions_path
            ON file_sessions(file_path, created_at)
        """)

        await db.commit()

        self._initialized = True

    async def create_session(self, metadata: Optional[dict] = None) -> str:
        """Создать новую сессию и вернуть session_id"""
        await self.initialize()

        session_id = str(uuid.uuid4())
        expires_at = _utcnow() + timedelta(seconds=MAPPING_TTL_SECONDS)

        db = await self._get_db()
        await db.execute(
            "INSERT INTO sessions (session_id, expires_at, metadata) VALUES (?, ?, ?)",
            (session_id, _to_iso(expires_at), json.dumps(metadata or {}))
        )
        await db.commit()

        # Инициализируем in-memory cache для сессии
        self._memory_cache[session_id] = {}

        return session_id

    async def get_or_create_session(self, session_id: Optional[str] = None) -> str:
        """Получить существующую сессию или создать новую"""
        if session_id:
            # Проверяем, существует ли сессия и не истекла ли
            db = await self._get_db()
            cursor = await db.execute(
                "SELECT expires_at FROM sessions WHERE session_id = ?",
                (session_id,)
            )
            row = await cursor.fetchone()

            if row:
                expires_at = _from_iso(row[0])
                if expires_at > _utcnow().replace(tzinfo=None):
                    # Сессия валидна, загружаем в cache если нужно
                    if session_id not in self._memory_cache:
                        await self._load_session_to_cache(session_id)
                    return session_id

        # Создаём новую сессию
        return await self.create_session()

    async def _load_session_to_cache(self, session_id: str):
        """Загрузить маппинги сессии в memory cache"""
        self._memory_cache[session_id] = {}

        db = await self._get_db()
        cursor = await db.execute(
            "SELECT token, original_value, entity_type, created_at FROM mappings WHERE session_id = ?",
            (session_id,)
        )
        async for row in cursor:
            token, original_value, entity_type, created_at = row
            self._memory_cache[session_id][token] = MappingEntry(
                token=token,
                original_value=original_value,
                entity_type=entity_type,
                session_id=session_id,
                created_at=_from_iso(created_at) if created_at else _utcnow().replace(tzinfo=None)
            )

    def _generate_token(self, entity_type: str, counter: int) -> str:
        """Сгенерировать токен для замены"""
        return f"[{entity_type}_{counter}]"

    def _next_counter(self, session_id: str) -> int:
        """Максимальный числовой суффикс среди токенов сессии (0, если пусто).

        Счётчик берётся от реальных токенов, а не от len(cache) — это
        устойчиво к пропускам нумерации и исключает перезапись маппингов.
        """
        return max(
            (_token_counter(t) for t in self._memory_cache.get(session_id, {})),
            default=0,
        )

    async def add_mapping(
        self,
        session_id: str,
        original_value: str,
        entity_type: str
    ) -> str:
        """
        Добавить маппинг и вернуть токен.

        Если значение уже есть в сессии, возвращает существующий токен.
        Генерация токена потокобезопасна для сессии и не перезаписывает
        существующие маппинги: счётчик берётся от максимального числового
        суффикса токенов, а при коллизии в БД используется INSERT с повторной
        попыткой (вместо INSERT OR REPLACE).
        """
        await self.initialize()

        # Сериализуем обращения к одной сессии: генерация токена из счётчика
        # не должна гоняться между конкурентными запросами с одним session_id.
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            # Подгружаем маппинги сессии, если кэш ещё не заполнен (например,
            # после рестарта), чтобы счётчик учитывал токены из БД.
            if session_id not in self._memory_cache:
                await self._load_session_to_cache(session_id)
            cache = self._memory_cache[session_id]

            # Идемпотентность: то же значение в сессии — тот же токен.
            for token, entry in cache.items():
                if entry.original_value == original_value:
                    return token

            counter = self._next_counter(session_id) + 1
            token = self._generate_token(entity_type, counter)
            while token in cache:
                counter += 1
                token = self._generate_token(entity_type, counter)

            # INSERT вместо OR REPLACE: при коллизии (например, гонка между
            # процессами на одной БД) пробуем следующий номер, а не затираем.
            db = await self._get_db()
            while True:
                try:
                    await db.execute(
                        "INSERT INTO mappings "
                        "(session_id, token, original_value, entity_type) "
                        "VALUES (?, ?, ?, ?)",
                        (session_id, token, original_value, entity_type),
                    )
                    await db.commit()
                    break
                except sqlite3.IntegrityError:
                    counter += 1
                    token = self._generate_token(entity_type, counter)
                    while token in cache:
                        counter += 1
                        token = self._generate_token(entity_type, counter)

            cache[token] = MappingEntry(
                token=token,
                original_value=original_value,
                entity_type=entity_type,
                session_id=session_id,
            )

            return token

    async def get_original_value(self, session_id: str, token: str) -> Optional[str]:
        """Получить оригинальное значение по токену"""
        # Сначала проверяем cache
        if session_id in self._memory_cache:
            if token in self._memory_cache[session_id]:
                return self._memory_cache[session_id][token].original_value

        # Иначе ищем в БД
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT original_value FROM mappings WHERE session_id = ? AND token = ?",
            (session_id, token)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_all_mappings(self, session_id: str) -> dict[str, str]:
        """Получить все маппинги для сессии (token -> original_value)"""
        if session_id in self._memory_cache:
            return {
                token: entry.original_value
                for token, entry in self._memory_cache[session_id].items()
            }

        # Загружаем из БД
        await self._load_session_to_cache(session_id)
        return {
            token: entry.original_value
            for token, entry in self._memory_cache.get(session_id, {}).items()
        }

    async def log_request(
        self,
        session_id: str,
        request_type: str,
        original_content: str,
        anonymized_content: str,
        response_content: Optional[str] = None,
        entities_found: Optional[list[Entity]] = None,
        processing_time_ms: float = 0.0,
        error: Optional[str] = None
    ):
        """Записать лог запроса"""
        await self.initialize()

        entities_json = json.dumps([e.model_dump() for e in (entities_found or [])], ensure_ascii=False)

        db = await self._get_db()
        await db.execute(
            """INSERT INTO logs 
            (session_id, request_type, original_content, anonymized_content, 
             response_content, entities_found, processing_time_ms, error) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                request_type,
                original_content,
                anonymized_content,
                response_content,
                entities_json,
                processing_time_ms,
                error
            )
        )
        await db.commit()

    async def get_logs(
        self,
        session_id: Optional[str] = None,
        limit: int = 100
    ) -> list[dict]:
        """Получить логи (опционально по session_id)"""
        await self.initialize()

        db = await self._get_db()

        if session_id:
            cursor = await db.execute(
                "SELECT * FROM logs WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )

        # Собираем dict по описанию курсора, не трогая row_factory общего
        # соединения (иначе состояние «протекает» в другие запросы).
        columns = [desc[0] for desc in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    async def cleanup_expired(self):
        """Удалить истёкшие сессии: записи БД, кэш и файлы на диске."""
        await self.initialize()

        now_iso = _to_iso(_utcnow())

        db = await self._get_db()

        # Сначала запоминаем истёкшие session_id (для чистки cache)
        cursor = await db.execute(
            "SELECT session_id FROM sessions WHERE expires_at < ?",
            (now_iso,)
        )
        expired_rows = await cursor.fetchall()
        expired_ids = {row[0] for row in expired_rows}

        # Удаляем истёкшие сессии и их маппинги
        await db.execute(
            "DELETE FROM mappings WHERE session_id IN (SELECT session_id FROM sessions WHERE expires_at < ?)",
            (now_iso,)
        )
        await db.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (now_iso,)
        )
        await db.commit()

        # Очищаем memory cache, лок-блоки и файлы на диске
        for sid in expired_ids:
            self._memory_cache.pop(sid, None)
            self._session_locks.pop(sid, None)
            # Анонимизированные копии и review-.md файлы на диске
            for base_dir in (ANONYMIZED_FILES_DIR, MAPPINGS_DIR):
                session_dir = base_dir / sid
                if session_dir.is_dir():
                    shutil.rmtree(session_dir, ignore_errors=True)

        if expired_ids:
            logger.info("Очищено истёкших сессий: %d", len(expired_ids))

    async def save_anonymized_file(
        self,
        session_id: str,
        original_filename: str,
        original_content: bytes,
        anonymized_content: bytes
    ) -> tuple[Path, Path]:
        """
        Сохранить оригинальный и анонимизированный файлы для оценки качества

        Returns:
            Кортеж (путь к оригиналу, путь к анонимизированному файлу)
        """
        session_dir = MAPPINGS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Генерируем уникальные имена
        timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
        base_name = Path(original_filename).stem
        ext = Path(original_filename).suffix

        original_path = session_dir / f"{base_name}_original_{timestamp}{ext}"
        anonymized_path = session_dir / f"{base_name}_anonymized_{timestamp}{ext}"

        # Сохраняем файлы
        original_path.write_bytes(original_content)
        anonymized_path.write_bytes(anonymized_content)

        return original_path, anonymized_path

    async def save_anonymized_text(
        self,
        session_id: str,
        text: str,
        prefix: str = "anonymized_request"
    ) -> Path:
        """
        Сохранить канонический анонимизированный текст запроса в
        data/anonymized_files/<session_id>/

        Returns:
            Путь к сохранённому файлу
        """
        session_dir = ANONYMIZED_FILES_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Микросекунды в имени — защита от коллизий при быстрых повторных запросах
        timestamp = _utcnow().strftime("%Y%m%d_%H%M%S_%f")
        path = session_dir / f"{prefix}_{timestamp}.md"
        path.write_text(text, encoding="utf-8")
        return path

    async def register_file_session(self, file_path: str, session_id: str) -> None:
        """Связать путь файла с сессией анонимизации (для частичной
        де-анонимизации: по пути файла находится сессия с маппингами)."""
        await self.initialize()
        db = await self._get_db()
        await db.execute(
            "INSERT OR IGNORE INTO file_sessions (file_path, session_id) "
            "VALUES (?, ?)",
            (str(file_path), session_id),
        )
        await db.commit()

    async def get_latest_session_for_file(self, file_path: str) -> Optional[str]:
        """Последняя сессия анонимизации, создававшая этот файл."""
        await self.initialize()
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT session_id FROM file_sessions "
            "WHERE file_path = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (str(file_path),),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def find_sessions_with_tokens(
        self, tokens: Sequence[str]
    ) -> List[str]:
        """Сессии, в маппингах которых есть хотя бы один из токенов
        (свежие первыми). Используется фоллбеком команды «деанонимизируй
        плейсхолдеры…», когда в диалоге нет маркеров
        [anonymizer:result:…] (багрепорт 2026-09-09: плейсхолдеры из старого
        диалога не деанонимизировались в новом чате)."""
        tokens = [t for t in tokens if t]
        if not tokens:
            return []
        await self.initialize()
        db = await self._get_db()
        placeholders = ",".join("?" for _ in tokens)
        cursor = await db.execute(
            f"SELECT DISTINCT session_id FROM mappings "
            f"WHERE token IN ({placeholders}) ORDER BY created_at DESC",
            list(tokens),
        )
        rows = await cursor.fetchall()
        seen: set = set()
        ordered: List[str] = []
        for (sid,) in rows:
            if sid not in seen:
                seen.add(sid)
                ordered.append(sid)
        return ordered

    async def get_files_for_session(self, session_id: str) -> List[str]:
        """Файлы, зарегистрированные за сессию (свежие первыми)."""
        await self.initialize()
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT file_path FROM file_sessions "
            "WHERE session_id = ? ORDER BY created_at DESC, id DESC",
            (session_id,),
        )
        return [r[0] for r in await cursor.fetchall()]

    async def get_all_sessions(self) -> list[dict]:
        """Возвращает список активных сессий с количеством маппингов."""
        await self.initialize()
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT session_id, created_at, expires_at FROM sessions "
            "WHERE expires_at > ? ORDER BY created_at DESC",
            (_to_iso(_utcnow()),),
        )
        sessions = []
        async for row in cursor:
            sid = row[0]
            map_cursor = await db.execute(
                "SELECT COUNT(*) FROM mappings WHERE session_id = ?", (sid,)
            )
            map_row = await map_cursor.fetchone()
            # Пути к .md файлам ревью
            session_dir = ANONYMIZED_FILES_DIR / sid
            review_files = [str(f) for f in session_dir.glob("*.md")] if session_dir.exists() else []
            sessions.append({
                "session_id": sid,
                "created_at": _from_iso(row[1]) if row[1] else _utcnow(),
                "expires_at": _from_iso(row[2]) if row[2] else _utcnow(),
                "mappings_count": map_row[0] if map_row else 0,
                "review_files": review_files,
            })
        return sessions

    async def close(self):
        """Закрыть соединения"""
        self._memory_cache.clear()
        self._session_locks.clear()
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:
                logger.warning("Ошибка закрытия БД: %s", e)
            self._db = None