"""Фоновая задача рассылок: раз в CHECK_EVERY_SECONDS ставит получателей в очередь уведомлений."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
import random
from datetime import datetime, time, timedelta, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.broadcasts import settings
from app.events import EventBus
from app.reminders.service import _MSK, is_quiet
from app.repositories import broadcasts as repo
from app.repositories import daily_promo as promo_repo

logger = logging.getLogger(__name__)

# Свой номер: 7_310_001 — схема, 7_310_003 — напоминания и архив.
_LOCK_ID = 7_310_004


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BroadcastService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        events: EventBus,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        daily_promo: bool = False,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._pool = pool
        self._events = events
        self._clock = clock
        self._sleep = sleep
        self._daily_promo = daily_promo
        self._rand = rand

    async def run(self) -> None:
        logger.info("Рассылки: проверка раз в %s с", settings.CHECK_EVERY_SECONDS)
        logger.info("Ежедневная рассылка: %s", "включена" if self._daily_promo else "выключена")
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Рассылки: проход не удался")
            await self._sleep(settings.CHECK_EVERY_SECONDS)

    async def run_once(self) -> int:
        """Один проход по всем рассылкам. Возвращает, сколько получателей поставлено в очередь."""
        now = self._clock()
        sent = 0
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_ID,))
            row = await cursor.fetchone()
            if not (row and row["ok"]):
                logger.info("Рассылки: проход уже идёт в другом процессе")
                return 0
            try:
                for broadcast in await repo.due_scheduled(conn, now):
                    sent += await self._send_scheduled(conn, broadcast)
                if not is_quiet(now, settings.QUIET_HOURS_MSK):
                    for broadcast in await repo.active_events(conn):
                        sent += await self._send_event(conn, broadcast, now)
                if self._daily_promo:
                    sent += await self._send_daily(conn, now)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
        return sent

    async def _send_scheduled(self, conn, broadcast: dict[str, Any]) -> int:
        bid = broadcast["id"]
        if broadcast["status"] == "scheduled":
            await repo.set_status(conn, bid, "sending")
            logger.info("Рассылка %s: начата отправка", bid)
        total = 0
        while True:
            current = await repo.get(conn, bid)
            if not current or current["status"] != "sending":
                # Отменили из CRM посреди отправки.
                logger.info("Рассылка %s: отправка остановлена (%s)", bid, current and current["status"])
                return total
            batch = await self._batch(
                conn, bid, lambda: repo.claim_scheduled(conn, current, limit=settings.BATCH_SIZE), current
            )
            if batch is None:
                return total
            if not batch:
                await repo.set_status(conn, bid, "done", finished=True)
                logger.info("Рассылка %s: отправлена, в этот проход %s", bid, total)
                return total
            total += len(batch)
            await self._sleep(settings.BATCH_PAUSE_SECONDS)

    async def _send_event(self, conn, broadcast: dict[str, Any], now: datetime) -> int:
        bid = broadcast["id"]
        total = 0
        while True:
            batch = await self._batch(
                conn,
                bid,
                lambda: repo.claim_event(conn, broadcast, now=now, limit=settings.BATCH_SIZE),
                broadcast,
            )
            if not batch:
                break
            total += len(batch)
            await self._sleep(settings.BATCH_PAUSE_SECONDS)
        if total:
            logger.info("Рассылка %s (%s): поставлено %s", bid, broadcast["event"], total)
        return total

    async def _batch(self, conn, bid: int, claim, broadcast: dict[str, Any]):
        """Журнал, счётчик и очередь одной транзакцией. None — пачка не удалась."""
        try:
            async with conn.transaction():
                batch = await claim()
                if batch:
                    await self._events.publish_notices(
                        batch,
                        broadcast["text"],
                        media_id=broadcast["media_id"],
                        content_type=broadcast["content_type"],
                    )
                    await repo.add_sent(conn, bid, len(batch))
                return batch
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Рассылка %s: пачка отменена, продолжу в следующий проход", bid)
            return None

    async def _send_daily(self, conn, now: datetime) -> int:
        """Ежедневное промо: раз в день по Москве в случайный момент окна, человеку не больше одного."""
        local = now.astimezone(_MSK)
        day = local.date()
        start_hour, end_hour = settings.DAILY_PROMO_WINDOW_MSK
        window_start = datetime.combine(day, time(start_hour), tzinfo=_MSK)
        offset = timedelta(hours=end_hour - start_hour) * min(max(self._rand(), 0.0), 0.999999)
        row = await promo_repo.ensure_day(conn, day, (window_start + offset).astimezone(timezone.utc))
        send_at = row["send_at"]
        if now < send_at or local.hour >= settings.DAILY_PROMO_UNTIL_MSK:
            return 0
        total = 0
        while True:
            try:
                async with conn.transaction():
                    batch = await promo_repo.claim(
                        conn, day, send_at, settings.DAILY_PROMO_PLATFORMS, limit=settings.BATCH_SIZE
                    )
                    if batch:
                        await self._events.publish_notices(
                            batch, settings.DAILY_PROMO_TEXT, screen=settings.DAILY_PROMO_SCREEN
                        )
                        await promo_repo.add_sent(conn, day, len(batch))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ежедневная рассылка %s: пачка отменена, продолжу в следующий проход", day)
                break
            if not batch:
                break
            total += len(batch)
            await self._sleep(settings.BATCH_PAUSE_SECONDS)
        if total:
            logger.info("Ежедневная рассылка %s: поставлено %s", day, total)
        return total
