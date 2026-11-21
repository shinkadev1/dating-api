"""Журнал инцидентов поиска на настоящем Postgres: TEST_DATABASE_URL или pgserver, иначе тесты пропускаются.

Запуск из chat_api/: TEST_DATABASE_URL=postgresql://user:pass@host:port/db python -m pytest tests/test_search_incidents_db.py
"""

import asyncio
import os
import sys
import unittest

import httpx
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings
from app.db import ensure_schema
from app.deps import current_client, get_chat
from app.peer import Peer
from app.routers import analytics as analytics_router
from app.routers import engine as engine_router
from app.services.chat import ChatService

sys.path.insert(0, os.path.dirname(__file__))
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

A = Peer("tg", "bot", 11)
B = Peer("tg", "bot", 22)
ENGINE_TOKEN = "engine-token-000003"


class FakeEvents:
    async def publish(self, peer, event):
        pass

    async def request_engine(self):
        pass


class SearchIncidentsDbTest(unittest.IsolatedAsyncioTestCase):
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
        await ensure_schema(self.pool)  # идемпотентность миграции 013
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE search_incidents, engine_presence, messages, message_archives, dialogs, chat_sessions, "
                "bot_users RESTART IDENTITY CASCADE"
            )
        settings = Settings(
            database_url=self.url,
            chat_api_service_token="service-token-0001",
            chat_api_app_token="app-token-00000002",
            chat_api_engine_token=ENGINE_TOKEN,
        )
        self.chat = ChatService(self.pool, FakeEvents(), settings)
        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(analytics_router.router, prefix="/api/v1")
        app.include_router(engine_router.router, prefix="/api/v1")
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_chat] = lambda: self.chat
        app.dependency_overrides[current_client] = lambda: "crm"
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)

    async def rows(self, query="SELECT * FROM search_incidents ORDER BY id", args=()):
        async with self.pool.connection() as conn:
            cursor = await conn.execute(query, args)
            return await cursor.fetchall()

    async def age_search(self, peer, seconds):
        """Человек ждёт уже `seconds` секунд."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET searching_since = NOW() - make_interval(secs => %s) "
                "WHERE platform = %s AND user_id = %s",
                (seconds, peer.platform, peer.user_id),
            )

    async def test_stop_from_search_writes_incident_with_snapshot(self):
        await self.chat.engine_status(Peer("engine", "acc1", 1), "any", None, False)
        await self.chat.search(A)
        await self.age_search(A, 120)
        result = await self.chat.stop(A)
        self.assertTrue(result.was_active)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["platform"], row["user_id"], row["mode"], row["outcome"]), ("tg", 11, "normal", "stopped"))
        self.assertGreaterEqual(row["wait_seconds"], 119)
        self.assertEqual(row["reason"], "bridge_not_hunting")
        self.assertEqual([a["id"] for a in row["snapshot"]["accounts"]], ["acc1"])
        self.assertEqual(row["snapshot"]["waiting"]["normal"], 0)

    async def test_quick_stop_and_no_incident_from_dialog(self):
        await self.chat.search(A)
        await self.chat.stop(A)
        self.assertEqual([r["reason"] for r in await self.rows()], ["left_quickly"])
        await self.chat.search(A)
        await self.chat.search(B)  # пара собралась
        await self.chat.stop(A)
        self.assertEqual(len(await self.rows()), 1)

    async def test_mode_change_writes_incident_and_same_mode_does_not(self):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO bot_users (platform, user_id, premium_until) VALUES ('tg', 11, NOW() + interval '1 day')"
            )
        await self.chat.search(A, "normal")
        await self.chat.search(A, "normal")
        self.assertEqual(await self.rows(), [])
        await self.chat.search(A, "adult")
        rows = await self.rows()
        self.assertEqual([(r["outcome"], r["mode"]) for r in rows], [("mode_changed", "normal")])

    async def test_hunt_and_status_drive_reason(self):
        engine = Peer("engine", "acc1", 1)
        await self.chat.engine_status(engine, "normal", "409 all bots unavailable", True)
        await self.chat.search(A)
        await self.age_search(A, 60)
        await self.chat.stop(A)
        self.assertEqual((await self.rows())[-1]["reason"], "all_accounts_unavailable")
        await self.chat.engine_status(engine, None, None, True)  # очистка ошибки
        await self.chat.search(A)
        await self.age_search(A, 60)
        await self.chat.engine_hunt(engine, "normal")
        await self.chat.stop(A)
        self.assertEqual((await self.rows())[-1]["reason"], "engine_searched_no_match")

    async def test_status_keeps_error_when_field_not_given(self):
        engine = Peer("engine", "acc1", 1)
        await self.chat.engine_status(engine, "adult", "limited", True)
        await self.chat.engine_status(engine, None, None, False)
        rows = await self.rows("SELECT mode, last_error, last_error_at FROM engine_presence")
        self.assertEqual((rows[0]["mode"], rows[0]["last_error"]), ("adult", "limited"))
        self.assertIsNotNone(rows[0]["last_error_at"])

    async def test_analytics_endpoint_filters_and_summary(self):
        async with self.pool.connection() as conn:
            for i, (platform, mode, reason, outcome) in enumerate(
                [
                    ("tg", "normal", "left_quickly", "stopped"),
                    ("tg", "adult", "no_account_for_mode", "stopped"),
                    ("vk", "normal", "no_account_for_mode", "mode_changed"),
                ]
            ):
                await conn.execute(
                    "INSERT INTO search_incidents (platform, user_id, mode, wait_seconds, outcome, reason) "
                    "VALUES (%s, %s, %s, 30, %s, %s)",
                    (platform, i + 1, mode, outcome, reason),
                )
        r = await self.api.get("/api/v1/analytics/incidents")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["summary"], {"left_quickly": 1, "no_account_for_mode": 2})
        self.assertEqual(len(body["items"]), 3)
        r = await self.api.get("/api/v1/analytics/incidents", params={"reason": "no_account_for_mode", "platform": "vk"})
        self.assertEqual([i["platform"] for i in r.json()["items"]], ["vk"])
        r = await self.api.get("/api/v1/analytics/incidents", params={"mode": "adult", "limit": 1})
        self.assertEqual(r.json()["items"][0]["mode"], "adult")
        r = await self.api.get("/api/v1/analytics/incidents", params={"limit": 2, "offset": 2})
        self.assertEqual((len(r.json()["items"]), r.json()["total"]), (1, 3))
        r = await self.api.get("/api/v1/analytics/incidents", params={"date_from": "2999-01-01"})
        self.assertEqual((r.json()["items"], r.json()["total"]), ([], 0))
        r = await self.api.get("/api/v1/analytics/incidents", params={"reason": "bogus"})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
