"""Напоминания на настоящем Postgres: TEST_DATABASE_URL или pgserver во временном каталоге."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import ensure_schema
from app.repositories import reminders

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
PLATFORMS = ["vk", "tg", "web"]


def _database_url():
    """(url, cleanup) или (None, None), если базу взять негде."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url, None
    try:
        import pgserver
    except ImportError:
        return None, None
    tmp = tempfile.TemporaryDirectory()
    try:
        server = pgserver.get_server(tmp.name, cleanup_mode="stop")
        url = server.get_uri()
    except Exception:
        tmp.cleanup()
        return None, None

    def cleanup():
        try:
            server.cleanup()
        finally:
            try:
                tmp.cleanup()
            except OSError:
                pass

    return url, cleanup


class ReminderDbTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.url, cls._cleanup = _database_url()

    @classmethod
    def tearDownClass(cls):
        if cls._cleanup:
            cls._cleanup()

    async def asyncSetUp(self):
        if not self.url:
            self.skipTest("нет Postgres: задайте TEST_DATABASE_URL или установите pgserver")
        self.pool = AsyncConnectionPool(
            self.url,
            min_size=1,
            max_size=3,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await self.pool.open(wait=True, timeout=60)
        self.addAsyncCleanup(self.pool.close)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE reminder_log, user_channels, chat_sessions, bot_users, channels RESTART IDENTITY CASCADE"
            )
        self._users = 0

    async def channel(self, platform="vk", external_id="-100"):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO channels (platform, external_id) VALUES (%s, %s) RETURNING id",
                (platform, external_id),
            )
            return (await cur.fetchone())["id"]

    async def link(self, channel_id, user_id, age, platform="vk"):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO bot_users (platform, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (platform, user_id),
            )
            await conn.execute(
                "INSERT INTO user_channels (user_platform, user_id, channel_id, last_seen_at) "
                "VALUES (%s, %s, %s, %s)",
                (platform, user_id, channel_id, NOW - age),
            )

    async def session(self, user_id, phase, bot_key="", platform="vk"):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (platform, bot_key, user_id, phase) "
                "VALUES (%s, %s, %s, %s)",
                (platform, bot_key, user_id, phase),
            )

    async def claim(self, platforms=None, limit=100, conn=None):
        args = dict(
            platforms=platforms or PLATFORMS,
            seen_before=NOW - timedelta(hours=24),
            seen_after=NOW - timedelta(days=30),
            logged_after=NOW - timedelta(hours=24),
            limit=limit,
        )
        if conn is not None:
            return await reminders.claim_batch(conn, **args)
        async with self.pool.connection() as c:
            return await reminders.claim_batch(c, **args)

    async def log_count(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM reminder_log")
            return (await cur.fetchone())["n"]

    async def test_schema_twice_and_table_exists(self):
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            self.assertTrue(await reminders.table_exists(conn))

    async def test_window(self):
        ch = await self.channel()
        await self.link(ch, 1, timedelta(hours=25))  # подходит
        await self.link(ch, 2, timedelta(hours=23))  # слишком свежий
        await self.link(ch, 3, timedelta(days=31))  # слишком давний
        await self.link(ch, 4, timedelta(days=29))  # подходит
        got = await self.claim()
        self.assertEqual(sorted(p.user_id for p in got), [1, 4])
        self.assertEqual({(p.platform, p.bot_key) for p in got}, {("vk", "-100")})

    async def test_platforms_filter(self):
        vk = await self.channel("vk", "-1")
        tg = await self.channel("tg", "bot")
        await self.link(vk, 1, timedelta(hours=30))
        await self.link(tg, 2, timedelta(hours=30), platform="tg")
        got = await self.claim(platforms=["tg"])
        self.assertEqual([(p.platform, p.bot_key, p.user_id) for p in got], [("tg", "bot", 2)])

    async def test_skips_searching_and_chatting_any_bot_key(self):
        ch = await self.channel()
        for uid in (1, 2, 3, 4):
            await self.link(ch, uid, timedelta(hours=30))
        await self.session(1, "searching", bot_key="-100")
        await self.session(2, "chatting", bot_key="-555")
        await self.session(3, "idle", bot_key="-100")
        got = await self.claim()
        self.assertEqual(sorted(p.user_id for p in got), [3, 4])

    async def test_session_of_other_platform_does_not_block(self):
        ch = await self.channel()
        await self.link(ch, 1, timedelta(hours=30))
        await self.session(1, "chatting", platform="tg", bot_key="bot")
        got = await self.claim()
        self.assertEqual([p.user_id for p in got], [1])

    async def test_log_blocks_for_24h_and_repeat_is_empty(self):
        ch = await self.channel()
        await self.link(ch, 1, timedelta(hours=30))
        await self.link(ch, 2, timedelta(hours=30))
        first = await self.claim()
        self.assertEqual(len(first), 2)
        self.assertEqual(await self.log_count(), 2)
        self.assertEqual(await self.claim(), [])
        # Запись старше 24 часов больше не мешает.
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE reminder_log SET sent_at = %s", (NOW - timedelta(hours=25),))
        self.assertEqual(len(await self.claim()), 2)

    async def test_limit(self):
        ch = await self.channel()
        for uid in range(1, 6):
            await self.link(ch, uid, timedelta(hours=30))
        self.assertEqual(len(await self.claim(limit=2)), 2)
        self.assertEqual(len(await self.claim(limit=10)), 3)

    async def test_rollback_leaves_no_rows(self):
        ch = await self.channel()
        await self.link(ch, 1, timedelta(hours=30))
        async with self.pool.connection() as conn:
            with self.assertRaises(RuntimeError):
                async with conn.transaction():
                    self.assertEqual(len(await self.claim(conn=conn)), 1)
                    raise RuntimeError("publish failed")
        self.assertEqual(await self.log_count(), 0)
        self.assertEqual(len(await self.claim()), 1)


if __name__ == "__main__":
    unittest.main()
