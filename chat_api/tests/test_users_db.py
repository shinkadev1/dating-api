"""Раздел «Пользователи» для Bot CRM на настоящем Postgres: TEST_DATABASE_URL или pgserver."""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import ensure_schema
from app.deps import current_client
from app.routers import users as users_router

sys.path.insert(0, os.path.dirname(__file__))
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

NOW = datetime.now(timezone.utc)


class UsersDbTest(unittest.IsolatedAsyncioTestCase):
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
            self.url, min_size=1, max_size=3, kwargs={"row_factory": dict_row, "autocommit": True}, open=False
        )
        await self.pool.open(wait=True, timeout=60)
        self.addAsyncCleanup(self.pool.close)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE broadcast_log, broadcasts, reminder_log, user_channels, chat_sessions, bot_users, "
                "dialogs, channels RESTART IDENTITY CASCADE"
            )
        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(users_router.router, prefix="/api/v1")
        self.kind = "crm"
        app.dependency_overrides[current_client] = lambda: self.kind
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)

        await self.sql("INSERT INTO channels (platform, external_id, title) VALUES ('tg', 'bot', 'TG бот'), ('vk', '-1', 'Группа')")
        rows = [
            # platform, id, handle, started, seen, searches, premium_until
            ("tg", 1, "anna", NOW - timedelta(days=40), NOW - timedelta(hours=1), 12, NOW + timedelta(days=3)),
            ("tg", 2, "boris", NOW - timedelta(hours=5), NOW - timedelta(hours=2), 1, None),
            ("tg", 3, None, NOW - timedelta(days=90), NOW - timedelta(days=60), 4, NOW - timedelta(days=50)),
            ("vk", 4, "vika", NOW - timedelta(days=2), NOW - timedelta(days=1), 3, None),
        ]
        for p, uid, h, st, seen, n, prem in rows:
            await self.sql(
                "INSERT INTO bot_users (platform, user_id, handle, started_at, last_seen_at, search_count, premium_until) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (p, uid, h, st, seen, n, prem),
            )
            await self.sql(
                "INSERT INTO user_channels (user_platform, user_id, channel_id, started_at, last_seen_at, search_count) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (p, uid, 1 if p == "tg" else 2, st, seen, n),
            )
        await self.sql("INSERT INTO chat_sessions (platform, bot_key, user_id, phase) VALUES ('tg', 'bot', 2, 'chatting')")
        await self.sql(
            "INSERT INTO dialogs (a_platform, a_bot_key, a_user_id, b_platform, b_bot_key, b_user_id, ended_at, end_reason) "
            "VALUES ('tg', 'bot', 1, 'tg', 'bot', 2, %s, 'reported'), ('tg', 'bot', 3, 'tg', 'bot', 1, %s, 'stopped')",
            (NOW, NOW),
        )
        await self.sql(
            "INSERT INTO broadcasts (title, text, platforms, kind, send_at, status) "
            "VALUES ('Акция', 'Скидка', ARRAY['tg'], 'at', %s, 'done')",
            (NOW,),
        )
        await self.sql("INSERT INTO broadcast_log (broadcast_id, user_platform, user_id, channel_id) VALUES (1, 'tg', 1, 1)")

    async def sql(self, query, args=()):
        async with self.pool.connection() as conn:
            await conn.execute(query, args)

    async def get(self, path, **params):
        r = await self.api.get("/api/v1" + path, params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_stats(self):
        s = await self.get("/users/stats", platforms="tg")
        self.assertEqual(s, {"total": 3, "premium": 1, "active_24h": 2, "active_7d": 2, "new_24h": 1,
                             "new_7d": 1, "searching": 0, "chatting": 1})

    async def test_list_filters_and_sort(self):
        r = await self.get("/users", platforms="tg")
        self.assertEqual((r["total"], [u["user_id"] for u in r["items"]]), (3, [1, 2, 3]))
        self.assertEqual(r["items"][1]["phase"], "chatting")
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", premium="yes"))["items"]], [1])
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", premium="ended"))["items"]], [3])
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", active="gone"))["items"]], [3])
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", sort="searches"))["items"]], [1, 3, 2])
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", q="@BOR"))["items"]], [2])
        self.assertEqual([u["user_id"] for u in (await self.get("/users", platforms="tg", q="3"))["items"]], [3])
        self.assertEqual((await self.get("/users", platforms="tg", q="%"))["total"], 0)
        page = await self.get("/users", platforms="tg", limit=2, offset=2)
        self.assertEqual((page["total"], [u["user_id"] for u in page["items"]]), (3, [3]))
        both = await self.api.get("/api/v1/users", params=[("platforms", "tg"), ("platforms", "vk")])
        self.assertEqual(both.json()["total"], 4)

    async def test_detail(self):
        u = await self.get("/users/tg/1")
        self.assertEqual((u["handle"], u["dialogs"], u["reported"], u["channels"]), ("anna", 2, 1, 1))
        self.assertEqual(u["chats"][0]["title"], "TG бот")
        self.assertEqual([(b["title"], b["times"]) for b in u["broadcasts"]], [("Акция", 1)])
        self.assertEqual((await self.api.get("/api/v1/users/tg/999")).status_code, 404)

    async def test_access(self):
        for kind, code in (("app", 403), ("tg_bot", 403), ("bot", 200)):
            self.kind = kind
            r = await self.api.get("/api/v1/users/stats", params={"platforms": "tg"})
            self.assertEqual(r.status_code, code, kind)


if __name__ == "__main__":
    unittest.main()
