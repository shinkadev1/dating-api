"""Выгрузка старых реплик в S3: JSONL+gzip по дням, удаление из базы только после проверки."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.repositories import messages as repo
from app.services.storage import ObjectStorage, StorageError

logger = logging.getLogger(__name__)

_CONTENT_TYPE = "application/gzip"


def archive_key(day: date, part: int) -> str:
    """Ключ объекта: archive/messages/ГГГГ/ММ/ДД.jsonl.gz, для повторных частей — ДД.partN.jsonl.gz."""
    suffix = "" if part == 1 else f".part{part}"
    return f"archive/messages/{day:%Y/%m/%d}{suffix}.jsonl.gz"


def _pack(rows: list[dict[str, Any]]) -> bytes:
    lines = (json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def _unpack(body: bytes) -> list[dict[str, Any]]:
    text = gzip.decompress(body).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


class MessageArchiver:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        storage: ObjectStorage,
        *,
        hot_days: int,
        batch_rows: int = 20_000,
        max_parts: int = 200,
    ) -> None:
        self._pool = pool
        self._storage = storage
        self._hot_days = hot_days
        self._batch_rows = batch_rows
        self._max_parts = max_parts

    async def run_forever(self, initial_delay: float = 300, interval: float = 86_400) -> None:
        """Первый проход через initial_delay секунд после старта, дальше раз в interval."""
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Архив сообщений: проход не удался")
            await asyncio.sleep(interval)

    async def run_once(self, today: date | None = None) -> int:
        """Один проход. Возвращает число выгруженных частей."""
        if not self._storage.enabled:
            logger.warning("S3 не настроен: архив сообщений пропущен")
            return 0
        cutoff = (today or datetime.now(UTC).date()) - timedelta(days=self._hot_days)
        async with self._pool.connection() as conn:
            if not await repo.try_archive_lock(conn):
                logger.info("Архив сообщений: другой проход уже идёт")
                return 0
            try:
                return await self._archive_days(conn, cutoff)
            finally:
                await repo.archive_unlock(conn)

    async def _archive_days(self, conn: Any, cutoff: date) -> int:
        done = 0
        while done < self._max_parts:
            oldest = await repo.oldest_message_at(conn)
            if oldest is None or oldest.astimezone(UTC).date() >= cutoff:
                break
            day = oldest.astimezone(UTC).date()
            start = datetime(day.year, day.month, day.day, tzinfo=UTC)
            rows = await repo.day_rows(conn, start, start + timedelta(days=1), self._batch_rows)
            if not rows:
                break
            part = await repo.next_part(conn, day)
            key = archive_key(day, part)
            ids = [int(row["id"]) for row in rows]
            try:
                body = await asyncio.to_thread(_pack, rows)
                await self._storage.put(key, body, _CONTENT_TYPE)
                if await self._storage.head(key) != len(body):
                    raise StorageError("размер объекта не совпал")
                stored = await asyncio.to_thread(_unpack, await self._storage.get(key))
                if len(stored) != len(rows) or stored[0]["id"] != ids[0] or stored[-1]["id"] != ids[-1]:
                    raise StorageError("содержимое объекта не совпало")
                await repo.commit_archive(
                    conn,
                    day=day,
                    part=part,
                    key=key,
                    rows_count=len(rows),
                    size_bytes=len(body),
                    ids=ids,
                )
            except Exception as exc:
                logger.error("Архив сообщений: %s не выгружен (%s), реплики оставлены в базе", key, type(exc).__name__)
                return done
            done += 1
            logger.info("Архив сообщений: %s — %s строк", key, len(rows))
        if done == 0:
            logger.info("Архив сообщений: старше %s выгружать нечего", cutoff)
        return done
