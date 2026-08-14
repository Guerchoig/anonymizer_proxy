"""
Менеджер очереди запросов, ожидающих ручного ревью

Реализует механизм приостановки запроса после анонимизации и ожидания
ручного подтверждения/редактирования от пользователя через расширение.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anonymizer_proxy.review_queue")


class ReviewRejectedError(Exception):
    """Запрос отклонён пользователем на этапе ручного ревью"""

    def __init__(self, reason: Optional[str] = None):
        self.reason = reason
        super().__init__(f"Запрос отклонён на ручном ревью: {reason or 'без причины'}")


@dataclass
class PendingReview:
    """Запрос, ожидающий ручного ревью"""
    session_id: str
    request_id: str
    anonymized_file_path: Path
    created_at: datetime = field(default_factory=datetime.now)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    edited_content: Optional[str] = None
    rejection_reason: Optional[str] = None


class ReviewQueue:
    """
    Менеджер очереди запросов, ожидающих ручного ревью.
    
    Поток обработки:
    1. Запрос приходит в режиме `review`
    2. Прокси анонимизирует, сохраняет .md, добавляет в pending
    3. Запрос блокируется на asyncio.Event.wait()
    4. Расширение опрашивает GET /api/review/pending
    5. Пользователь редактирует .md, жмёт "Отправить"
    6. POST /api/review/approve → event.set() → запрос разблокируется
    7. Прокси отправляет отредактированный контент в облако
    """
    
    def __init__(self):
        self._pending: dict[str, PendingReview] = {}  # request_id → PendingReview
        self._lock = asyncio.Lock()
    
    async def add_pending(
        self,
        session_id: str,
        request_id: str,
        anonymized_file_path: Path,
    ) -> PendingReview:
        """Добавить запрос в очередь ожидания ревью"""
        async with self._lock:
            review = PendingReview(
                session_id=session_id,
                request_id=request_id,
                anonymized_file_path=anonymized_file_path,
            )
            self._pending[request_id] = review
            logger.info(
                "Запрос %s добавлен в очередь ревью (session=%s, file=%s)",
                request_id, session_id, anonymized_file_path,
            )
            return review
    
    async def get_pending_list(self) -> list[dict]:
        """Получить список запросов, ожидающих ревью"""
        async with self._lock:
            return [
                {
                    "request_id": review.request_id,
                    "session_id": review.session_id,
                    "anonymized_file_path": str(review.anonymized_file_path),
                    "created_at": review.created_at.isoformat(),
                }
                for review in self._pending.values()
                if not review.event.is_set()
            ]
    
    async def approve(
        self,
        request_id: str,
        edited_content: Optional[str] = None,
    ) -> bool:
        """
        Одобрить запрос и разблокировать его.
        
        Args:
            request_id: ID запроса
            edited_content: Отредактированный контент (если None — использовать оригинальный .md)
        
        Returns:
            True если запрос найден и одобрен, False иначе
        """
        async with self._lock:
            review = self._pending.get(request_id)
            if not review:
                logger.warning("Запрос %s не найден в очереди ревью", request_id)
                return False
            
            if review.event.is_set():
                logger.warning("Запрос %s уже обработан", request_id)
                return False
            
            review.approved = True
            review.edited_content = edited_content
            review.event.set()
            
            logger.info(
                "Запрос %s одобрен (edited=%s)",
                request_id,
                edited_content is not None,
            )
            return True
    
    async def reject(self, request_id: str, reason: Optional[str] = None) -> bool:
        """
        Отклонить запрос.
        
        Args:
            request_id: ID запроса
            reason: Причина отклонения
        
        Returns:
            True если запрос найден и отклонён, False иначе
        """
        async with self._lock:
            review = self._pending.get(request_id)
            if not review:
                logger.warning("Запрос %s не найден в очереди ревью", request_id)
                return False
            
            if review.event.is_set():
                logger.warning("Запрос %s уже обработан", request_id)
                return False
            
            review.approved = False
            review.rejection_reason = reason
            review.event.set()
            
            logger.info("Запрос %s отклонён: %s", request_id, reason)
            return True
    
    async def wait_for_decision(self, request_id: str, timeout: Optional[float] = None) -> PendingReview:
        """
        Ожидать решения по запросу.
        
        Args:
            request_id: ID запроса
            timeout: Таймаут ожидания в секундах (None = без таймаута)
        
        Returns:
            PendingReview с результатом (approved/rejection_reason)
        
        Raises:
            KeyError: если запрос не найден
            asyncio.TimeoutError: если истёк таймаут
        """
        async with self._lock:
            review = self._pending.get(request_id)
            if not review:
                raise KeyError(f"Запрос {request_id} не найден в очереди ревью")
        
        # Ожидаем сигнала (вне lock, чтобы не блокировать другие операции)
        await asyncio.wait_for(review.event.wait(), timeout=timeout)
        
        return review
    
    async def cleanup_old(self, max_age_seconds: int = 3600):
        """Очистить старые обработанные запросы"""
        now = datetime.now()
        async with self._lock:
            to_remove = [
                request_id
                for request_id, review in self._pending.items()
                if review.event.is_set()
                and (now - review.created_at).total_seconds() > max_age_seconds
            ]
            for request_id in to_remove:
                del self._pending[request_id]
            
            if to_remove:
                logger.info("Очищено %d старых запросов из очереди ревью", len(to_remove))
