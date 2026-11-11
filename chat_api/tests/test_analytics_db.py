"""Аналитика для Bot CRM на настоящем Postgres: TEST_DATABASE_URL или pgserver."""

import asyncio
import os
import sys
import unittest
from datetime import datetime, time, timedelta, timezone

import httpx
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import ensure_schema
from app.deps import current_client
from app.peer import Peer
from app.repositories import sessions
from app.routers import analytics as analytics_router

sys.path.insert(0, os.path.dirname(__file__))
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

MSK = timezone(timedelta(hours=3))


class AnalyticsDbTest(unittest.IsolatedAsyncioTestCase):
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
                "TRUNCATE user_activity, broadcast_log, broadcasts, reminder_log, user_channels, chat_sessions, "
                "bot_users, dialogs, channels RESTART IDENTITY CASCADE"
            )
        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(analytics_router.router, prefix="/api/v1")
        app.dependency_overrides[current_client] = lambda: "crm"
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)
        self.today = datetime.now(MSK).date()

    def at(self, days_ago):
        """Полдень по Москве N дней назад."""
        return datetime.combine(self.today - timedelta(days=days_ago), time(12), MSK)

    async def sql(self, query, args=()):
        async with self.pool.connection() as conn:
            await conn.execute(query, args)

    async def person(self, uid, joined, active_days=(), platform="tg", searches=0, premium=None):
        await self.sql(
            "INSERT INTO bot_users (platform, user_id, started_at, last_seen_at, search_count, premium_until) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (platform, uid, self.at(joined), self.at(min((joined, *active_days))), searches, premium),
        )
        for d in (joined, *active_days):
            await self.sql(
                "INSERT INTO user_activity (platform, user_id, day) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (platform, uid, self.today - timedelta(days=d)),
            )

    async def test_numbers(self):
        # пришли сегодня, вчера (вернулся сегодня — D1), 8 дней назад (вернулся через 7 дней), 40 дней назад
        await self.person(1, 0, searches=2)
        await self.person(2, 1, active_days=(0,), searches=5, premium=datetime.now(MSK) + timedelta(days=5))
        await self.person(3, 8, active_days=(1,), searches=1)
        await self.person(4, 40, searches=0, premium=datetime.now(MSK) - timedelta(days=1))
        await self.person(9, 0, platform="vk")
        for a, b in ((1, 2), (2, 3), (2, 1), (3, 1)):
            await self.sql(
                "INSERT INTO dialogs (a_platform, a_bot_key, a_user_id, b_platform, b_bot_key, b_user_id, started_at, ended_at, end_reason) "
                "VALUES ('tg', 'bot', %s, 'tg', 'bot', %s, %s, %s, %s)",
                (a, b, self.at(0), self.at(0) + timedelta(minutes=10), "reported" if a == 3 else "stopped"),
            )
        r = await self.api.get("/api/v1/analytics", params={"platforms": "tg", "days": 14})
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        s = a["summary"]
        self.assertEqual((s["users"], s["new_1"], s["new_1_prev"], s["new_7"], s["new_30"], s["new_30_prev"]), (4, 1, 1, 2, 3, 1))
        self.assertEqual((s["premium_active"], s["premium_ever"]), (1, 2))
        self.assertEqual((s["dau"], s["wau"], s["mau"]), (2, 3, 3))
        self.assertEqual(len(a["daily"]), 14)
        last = a["daily"][-1]
        self.assertEqual((last["new"], last["active"], last["dialogs"]), (1, 2, 4))
        # D1: люди 2, 3, 4 (пришедшие не позже вчера); вернулся на следующий день только 2.
        self.assertEqual(a["retention"]["d1"], {"base": 3, "kept": 1})
        # D7: 3 и 4 (пришли не меньше 7 дней назад); 3 был ровно через 7 дней.
        self.assertEqual(a["retention"]["d7"], {"base": 2, "kept": 1})
        self.assertEqual([f["users"] for f in a["funnel"]], [4, 3, 3, 2, 2])
        e = a["engagement"]
        self.assertEqual((e["dialogs_30"], e["reported_30"], round(e["median_minutes"])), (4, 1, 10))
        week_sizes = {c["week"]: (c["size"], c["kept"]) for c in a["cohorts"]}
        self.assertTrue(all(len(k) >= 1 and k[0] <= size for size, k in week_sizes.values()))
        self.assertEqual(sum(size for size, _ in week_sizes.values()), 4)  # 8 недель = 56 дней: все четверо внутри

    async def test_remember_writes_activity_once_per_day(self):
        peer = Peer("tg", "bot", 77)
        async with self.pool.connection() as conn:
            await sessions.touch_user(conn, peer)
            await sessions.record_search(conn, peer)
            cur = await conn.execute("SELECT COUNT(*) AS n FROM user_activity WHERE user_id = 77")
            self.assertEqual((await cur.fetchone())["n"], 1)

    async def test_backfill_from_history(self):
        await self.sql("INSERT INTO bot_users (platform, user_id, started_at, last_seen_at) VALUES ('tg', 5, %s, %s)", (self.at(10), self.at(2)))
        await self.sql(
            "INSERT INTO dialogs (a_platform, a_bot_key, a_user_id, b_platform, b_bot_key, b_user_id, started_at) "
            "VALUES ('tg', 'bot', 5, 'engine', 'x', 1, %s)",
            (self.at(6),),
        )
        await ensure_schema(self.pool)  # таблица пустая — заполняется из истории
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT platform, day FROM user_activity WHERE platform = %s ORDER BY day", ("tg",))
            rows = await cur.fetchall()
        self.assertEqual([(r["platform"], (self.today - r["day"]).days) for r in rows], [("tg", 10), ("tg", 6), ("tg", 2)])


if __name__ == "__main__":
    unittest.main()
