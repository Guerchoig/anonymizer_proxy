"""
Хранилище маппингов токенов и оригинальных значений
Использует SQLite (WAL) для персистентности и in-memory cache для быстрого доступа
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
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


class MappingStore:
    """Хранилище маппингов для анонимизации/де-анонимизации"""

    def __init__(self):
        self._db_path = DB_PATH
        self._memory_cache: dict[str, dict[str, MappingEntry]] = {}  # session_id -> {token -> entry}
        self._initialized = False
        self._db: Optional[aiosqlite.Connection] = None

    async def _get_db(self) -> aiosqlite.Connection:
        """
        Одно разделяемое соединение (вместо нового на каждую операцию).
        WAL-режим позволяет параллельные чтения при записи.
        """
        if self._db is None:
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

    async def add_mapping(
        self,
        session_id: str,
        original_value: str,
        entity_type: str
    ) -> str:
        """
        Добавить маппинг и вернуть токен

        Если значение уже есть в сессии, возвращает существующий токен
        """
        await self.initialize()

        # Проверяем, есть ли уже такое значение в сессии
        if session_id in self._memory_cache:
            for token, entry in self._memory_cache[session_id].items():
                if entry.original_value == original_value:
                    return token

        # Генерируем новый токен (счётчик с запасом против коллизий)
        counter = len(self._memory_cache.get(session_id, {})) + 1
        token = self._generate_token(entity_type, counter)
        while session_id in self._memory_cache and token in self._memory_cache[session_id]:
            counter += 1
            token = self._generate_token(entity_type, counter)

        # Сохраняем в БД
        db = await self._get_db()
        await db.execute(
            "INSERT OR REPLACE INTO mappings (session_id, token, original_value, entity_type) VALUES (?, ?, ?, ?)",
            (session_id, token, original_value, entity_type)
        )
        await db.commit()

        # Сохраняем в cache
        if session_id not in self._memory_cache:
            self._memory_cache[session_id] = {}

        self._memory_cache[session_id][token] = MappingEntry(
            token=token,
            original_value=original_value,
            entity_type=entity_type,
            session_id=session_id
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
        db.row_factory = aiosqlite.Row

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

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def cleanup_expired(self):
        """Удалить истёкшие сессии и их маппинги"""
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

        # Очищаем memory cache
        for sid in expired_ids:
            self._memory_cache.pop(sid, None)

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
        Сохранить анонимизированный текст (например, запрос в режиме
        anonymize_only) в data/anonymized_files/<session_id>/

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
            sessions.append({
                "session_id": sid,
                "created_at": _from_iso(row[1]) if row[1] else _utcnow(),
                "expires_at": _from_iso(row[2]) if row[2] else _utcnow(),
                "mappings_count": map_row[0] if map_row else 0,
            })
        return sessions

    async def close(self):
        """Закрыть соединения"""
        self._memory_cache.clear()
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:
                logger.warning("Ошибка закрытия БД: %s", e)
            self._db = None