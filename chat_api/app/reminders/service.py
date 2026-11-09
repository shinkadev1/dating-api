"""Фоновая задача напоминаний: раз в CHECK_EVERY_MINUTES ставит рассылку в очередь событий."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from psycopg_pool import AsyncConnectionPool

from app.events import EventBus
from app.media_types import MediaRejected
from app.peer import Peer
from app.reminders import content, settings
from app.repositories import reminders as reminders_repo
from app.services.media import MediaService, StorageDisabled
from app.services.storage import StorageError

logger = logging.getLogger(__name__)

_LOCK_ID = 7_310_003
_MSK = timezone(timedelta(hours=3))
_FIRST_RUN_DELAY = 60
_MAX_TEXT = 4096


def is_quiet(now_utc: datetime, quiet: tuple[int, int]) -> bool:
    """Попадает ли момент в тихие часы по Москве: [начало, конец), можно через полночь."""
    start, end = quiet
    hour = now_utc.astimezone(_MSK).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def window(now_utc: datetime) -> tuple[datetime, datetime, datetime]:
    """Границы выборки: (не позже, не раньше) последней активности и начало окна журнала."""
    seen_before = now_utc - timedelta(hours=settings.DELAY_HOURS)
    seen_after = now_utc - timedelta(days=settings.MAX_INACTIVE_DAYS)
    logged_after = now_utc - timedelta(hours=settings.MIN_INTERVAL_HOURS)
    return seen_before, seen_after, logged_after


def valid_text(text: str) -> bool:
    return bool(text and text.strip()) and len(text) <= _MAX_TEXT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReminderService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        events: EventBus,
        media: MediaService,
        *,
        repo=reminders_repo,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pool = pool
        self._events = events
        self._media = media
        self._repo = repo
        self._clock = clock
        self._sleep = sleep
        self._photo_hash: str | None = None
        self._photo_cache: tuple[str, str] | None = None

    async def run(self) -> None:
        if not settings.ENABLED:
            await self._log_disabled()
            return
        logger.info(
            "Напоминания включены: через %s ч, пауза %s ч, до %s дн, тихие часы МСК %s, "
            "платформы %s, проверка раз в %s мин",
            settings.DELAY_HOURS,
            settings.MIN_INTERVAL_HOURS,
            settings.MAX_INACTIVE_DAYS,
            settings.QUIET_HOURS_MSK,
            ",".join(settings.PLATFORMS),
            settings.CHECK_EVERY_MINUTES,
        )
        await self._sleep(_FIRST_RUN_DELAY)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Напоминания: проход не удался")
            await self._sleep(settings.CHECK_EVERY_MINUTES * 60)

    async def _log_disabled(self) -> None:
        state = "нет"
        try:
            async with self._pool.connection() as conn:
                if await self._repo.table_exists(conn):
                    state = "есть"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Напоминания: не проверил журнал reminder_log: %s", exc)
        logger.info("Напоминания выключены (ENABLED=False), журнал reminder_log: %s", state)

    async def run_once(self) -> int:
        now = self._clock()
        if is_quiet(now, settings.QUIET_HOURS_MSK):
            return 0
        if not valid_text(content.TEXT):
            logger.error("Напоминания: текст пустой или длиннее %s символов", _MAX_TEXT)
            return 0
        media_id, content_type = await self._photo()
        seen_before, seen_after, logged_after = window(now)
        sent = 0
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_ID,))
            row = await cursor.fetchone()
            if not (row and row["ok"]):
                logger.info("Напоминания: проход уже идёт в другом процессе")
                return 0
            try:
                while True:
                    try:
                        async with conn.transaction():
                            batch = await self._repo.claim_batch(
                                conn,
                                platforms=list(settings.PLATFORMS),
                                seen_before=seen_before,
                                seen_after=seen_after,
                                logged_after=logged_after,
                                limit=settings.BATCH_SIZE,
                            )
                            if not batch:
                                break
                            await self._events.publish_notices(
                                batch,
                                content.TEXT,
                                media_id=media_id,
                                content_type=content_type,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Напоминания: пачка отменена, проход остановлен")
                        break
                    sent += len(batch)
                    await self._sleep(settings.BATCH_PAUSE_SECONDS)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
        if sent:
            logger.info("Напоминания: поставлено %s в очередь", sent)
        return sent

    async def _photo(self) -> tuple[str | None, str | None]:
        path = settings.PHOTO_PATH
        try:
            if not path.is_file():
                return None, None
            data = await asyncio.to_thread(path.read_bytes)
            digest = hashlib.sha256(data).hexdigest()
            if self._photo_hash == digest and self._photo_cache:
                return self._photo_cache
            saved = await self._media.save(Peer("web", "broadcast", 0), "photo", data)
        except (StorageDisabled, MediaRejected, StorageError, OSError) as exc:
            logger.warning("Напоминания: фото не загружено, уйдёт только текст (%r)", exc)
            return None, None
        self._photo_hash = digest
        self._photo_cache = (str(saved.id), saved.content_type)
        return self._photo_cache
