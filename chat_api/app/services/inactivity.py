"""Фоновое закрытие диалогов, где никто не писал DIALOG_INACTIVITY_MINUTES минут."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool

from app.peer import Peer
from app.repositories import sessions as repo
from app.services.chat import ChatService

logger = logging.getLogger(__name__)

_LOCK_ID = 7_310_005
_FIRST_RUN_DELAY = 30
CHECK_EVERY_SECONDS = 120
# Сколько сессий выбираем за запрос и сколько закрываем за проход; остаток бэклога уйдёт на следующих проходах.
BATCH_SIZE = 200
MAX_PER_RUN = 2000


class InactivitySweeper:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        chat: ChatService,
        *,
        minutes: int,
        silent_hours: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pool = pool
        self._chat = chat
        self._idle = minutes * 60.0
        self._silent = silent_hours * 3600.0
        self._enabled = minutes > 0
        self._sleep = sleep

    async def run(self) -> None:
        if not self._enabled:
            logger.info("Автозакрытие молчащих диалогов выключено (DIALOG_INACTIVITY_MINUTES=0)")
            return
        logger.info(
            "Автозакрытие диалогов: через %s мин, тихо при простое дольше %s ч, проверка раз в %s с",
            int(self._idle // 60),
            self._silent / 3600,
            CHECK_EVERY_SECONDS,
        )
        await self._sleep(_FIRST_RUN_DELAY)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Автозакрытие диалогов: проход не удался")
            await self._sleep(CHECK_EVERY_SECONDS)

    async def run_once(self) -> int:
        """Один проход под advisory lock. Возвращает число закрытых диалогов."""
        closed = 0
        failed: set[Peer] = set()
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_ID,))
            row = await cursor.fetchone()
            if not (row and row["ok"]):
                return 0
            try:
                while closed < MAX_PER_RUN:
                    found = await repo.find_inactive(conn, self._idle, BATCH_SIZE)
                    candidates = [p for p in found if p not in failed]
                    if not candidates:
                        break
                    progressed = False
                    for peer in candidates:
                        try:
                            if await self._chat.close_inactive(peer, self._idle, self._silent):
                                closed += 1
                                progressed = True
                            else:
                                failed.add(peer)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            failed.add(peer)
                            logger.exception("Автозакрытие: не закрыл диалог %s", peer)
                    if not progressed:
                        break
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
        if closed:
            logger.info("Автозакрытие диалогов: закрыто %s", closed)
        return closed
