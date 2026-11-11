"""Рассылки из CRM на настоящем Postgres: TEST_DATABASE_URL или pgserver во временном каталоге."""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.broadcasts import settings as bsettings
from app.broadcasts.service import BroadcastService
from app.db import ensure_schema
from app.deps import current_client
from app.routers import broadcasts as broadcasts_router

sys.path.insert(0, os.path.dirname(__file__))
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Полдень по Москве: не тихие часы.
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


class FakeBus:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def publish_notices(self, recipients, text, *, media_id=None, content_type=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.sent.extend((p.platform, p.bot_key, p.user_id, text) for p in recipients)


async def _no_sleep(_):
    return None


class BroadcastDbTest(unittest.IsolatedAsyncioTestCase):
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
        await ensure_schema(self.pool)  # файл идемпотентный
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE broadcast_log, broadcasts, reminder_log, user_channels, chat_sessions, bot_users, "
                "dialogs, channels RESTART IDENTITY CASCADE"
            )
        self.now = NOW
        self.bus = FakeBus()
        self.service = BroadcastService(self.pool, self.bus, clock=lambda: self.now, sleep=_no_sleep)

        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(broadcasts_router.router, prefix="/api/v1")
        self.client_kind = "crm"
        app.dependency_overrides[current_client] = lambda: self.client_kind
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)

    # ---------- данные ----------

    async def sql(self, query, args=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, args)
            return await cur.fetchall() if cur.description else None

    async def channel(self, platform="vk", external_id="-100"):
        rows = await self.sql(
            "INSERT INTO channels (platform, external_id) VALUES (%s, %s) RETURNING id", (platform, external_id)
        )
        return rows[0]["id"]

    async def user(self, channel_id, user_id, platform="vk", started=None, seen=None, premium_until=None):
        await self.sql(
            "INSERT INTO bot_users (platform, user_id, premium_until) VALUES (%s, %s, %s) "
            "ON CONFLICT (platform, user_id) DO UPDATE SET premium_until = EXCLUDED.premium_until",
            (platform, user_id, premium_until),
        )
        await self.sql(
            "INSERT INTO user_channels (user_platform, user_id, channel_id, started_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (platform, user_id, channel_id, started or NOW - timedelta(days=10), seen or NOW - timedelta(days=1)),
        )

    async def create(self, **body):
        r = await self.api.post("/api/v1/broadcasts", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    async def get(self, bid):
        return (await self.api.get(f"/api/v1/broadcasts/{bid}")).json()

    def ids(self):
        return sorted(u for _, _, u, _ in self.bus.sent)

    # ---------- по времени ----------

    async def test_scheduled_sends_once_to_selected_platforms(self):
        vk, tg = await self.channel("vk", "-1"), await self.channel("tg", "bot")
        for uid in (1, 2, 3):
            await self.user(vk, uid)
        await self.user(tg, 9, platform="tg")
        b = await self.create(text="Привет всем", platforms=["vk"], kind="at", send_at=(NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(b["status"], "scheduled")

        self.assertEqual(await self.service.run_once(), 0)  # ещё рано
        self.now = NOW + timedelta(hours=1, minutes=1)
        bsettings.BATCH_SIZE, old = 2, bsettings.BATCH_SIZE
        try:
            self.assertEqual(await self.service.run_once(), 3)
        finally:
            bsettings.BATCH_SIZE = old
        self.assertEqual(self.ids(), [1, 2, 3])
        b = await self.get(b["id"])
        self.assertEqual((b["status"], b["sent_count"]), ("done", 3))
        self.assertEqual(await self.service.run_once(), 0)  # повторно не уходит

    async def test_scheduled_resumes_after_failed_batch(self):
        vk = await self.channel()
        await self.user(vk, 1)
        await self.user(vk, 2)
        b = await self.create(text="x", platforms=["vk"], kind="at", send_at=NOW.isoformat())
        self.bus.fail = True
        self.assertEqual(await self.service.run_once(), 0)
        self.assertEqual((await self.get(b["id"]))["status"], "sending")
        rows = await self.sql("SELECT count(*) AS n FROM broadcast_log")
        self.assertEqual(rows[0]["n"], 0)  # журнал откатился вместе с пачкой
        self.bus.fail = False
        self.assertEqual(await self.service.run_once(), 2)
        self.assertEqual((await self.get(b["id"]))["status"], "done")

    async def test_cancel_scheduled(self):
        vk = await self.channel()
        await self.user(vk, 1)
        b = await self.create(text="x", platforms=["vk"], kind="at", send_at=(NOW + timedelta(hours=1)).isoformat())
        r = await self.api.patch(f"/api/v1/broadcasts/{b['id']}", json={"status": "cancelled"})
        self.assertEqual(r.json()["status"], "cancelled")
        self.now = NOW + timedelta(hours=2)
        self.assertEqual(await self.service.run_once(), 0)
        r = await self.api.patch(f"/api/v1/broadcasts/{b['id']}", json={"text": "y"})
        self.assertEqual(r.status_code, 409)

    # ---------- после события ----------

    async def test_event_only_after_activation_and_delay(self):
        vk = await self.channel()
        b = await self.api.post(
            "/api/v1/broadcasts",
            json={"text": "Как вам чат?", "platforms": ["vk"], "kind": "event", "event": "first_seen", "delay_minutes": 60},
        )
        b = b.json()
        await self.sql("UPDATE broadcasts SET active_since = %s", (NOW - timedelta(hours=5),))
        await self.user(vk, 1, started=NOW - timedelta(days=1))  # до активации — не получает
        await self.user(vk, 2, started=NOW - timedelta(hours=2))  # прошло 2 ч — получает
        await self.user(vk, 3, started=NOW - timedelta(minutes=30))  # пауза ещё не прошла
        self.assertEqual(await self.service.run_once(), 1)
        self.assertEqual(self.ids(), [2])
        self.now = NOW + timedelta(minutes=31)
        self.assertEqual(await self.service.run_once(), 1)
        self.assertEqual(self.ids(), [2, 3])
        self.now = NOW + timedelta(hours=5)
        self.assertEqual(await self.service.run_once(), 0)
        self.assertEqual((await self.get(b["id"]))["sent_count"], 2)

    async def test_event_skips_active_dialog_and_quiet_hours(self):
        vk = await self.channel()
        await self.create(text="x", platforms=["vk"], kind="event", event="last_seen", delay_minutes=60)
        await self.sql("UPDATE broadcasts SET active_since = %s", (NOW - timedelta(days=1),))
        await self.user(vk, 1, seen=NOW - timedelta(hours=3))
        await self.user(vk, 2, seen=NOW - timedelta(hours=3))
        await self.sql("INSERT INTO chat_sessions (platform, bot_key, user_id, phase) VALUES ('vk', '-100', 2, 'chatting')")
        night = NOW.replace(hour=22)  # 01:00 по Москве
        self.now = night
        self.assertEqual(await self.service.run_once(), 0)
        self.now = NOW
        self.assertEqual(await self.service.run_once(), 1)
        self.assertEqual(self.ids(), [1])

    async def test_event_repeat(self):
        vk = await self.channel()
        once = await self.create(text="once", platforms=["vk"], kind="event", event="last_seen", delay_minutes=60)
        again = await self.create(text="again", platforms=["vk"], kind="event", event="last_seen", delay_minutes=60, repeat=True)
        await self.sql("UPDATE broadcasts SET active_since = %s", (NOW - timedelta(days=5),))
        await self.user(vk, 1, seen=NOW - timedelta(hours=2))
        self.assertEqual(await self.service.run_once(), 2)
        # Человек вернулся и снова пропал: повторная уходит только у repeat=true.
        await self.sql("UPDATE user_channels SET last_seen_at = %s", (NOW + timedelta(hours=1),))
        self.now = NOW + timedelta(hours=3)
        self.assertEqual(await self.service.run_once(), 1)
        self.assertEqual([t for *_, t in self.bus.sent], ["once", "again", "again"])
        self.assertEqual((await self.get(once["id"]))["sent_count"], 1)
        self.assertEqual((await self.get(again["id"]))["sent_count"], 2)

    async def test_dialog_ended_and_premium_ended(self):
        vk = await self.channel()
        await self.create(text="dialog", platforms=["vk"], kind="event", event="dialog_ended", delay_minutes=10)
        await self.create(text="premium", platforms=["vk"], kind="event", event="premium_ended", delay_minutes=0)
        await self.sql("UPDATE broadcasts SET active_since = %s", (NOW - timedelta(days=5),))
        await self.user(vk, 1, premium_until=NOW - timedelta(hours=1))
        await self.user(vk, 2, premium_until=NOW + timedelta(days=3))
        await self.sql(
            "INSERT INTO dialogs (a_platform, a_bot_key, a_user_id, b_platform, b_bot_key, b_user_id, ended_at) "
            "VALUES ('vk', '-100', 3, 'vk', '-100', 2, %s)",
            (NOW - timedelta(minutes=20),),
        )
        self.assertEqual(await self.service.run_once(), 2)
        self.assertEqual(sorted((u, t) for _, _, u, t in self.bus.sent), [(1, "premium"), (2, "dialog")])

    async def test_paused_event_does_not_send(self):
        vk = await self.channel()
        b = await self.create(text="x", platforms=["vk"], kind="event", event="last_seen", delay_minutes=0)
        await self.sql("UPDATE broadcasts SET active_since = %s", (NOW - timedelta(days=5),))
        await self.user(vk, 1, seen=NOW - timedelta(hours=1))
        r = await self.api.patch(f"/api/v1/broadcasts/{b['id']}", json={"status": "paused"})
        self.assertEqual(r.json()["status"], "paused")
        self.assertEqual(await self.service.run_once(), 0)
        await self.api.patch(f"/api/v1/broadcasts/{b['id']}", json={"status": "active"})
        self.assertEqual(await self.service.run_once(), 1)

    # ---------- API ----------

    async def test_api_validation_audience_and_access(self):
        vk, tg = await self.channel("vk", "-1"), await self.channel("tg", "bot")
        await self.user(vk, 1)
        await self.user(vk, 2)
        await self.user(tg, 3, platform="tg")
        r = await self.api.get("/api/v1/broadcasts/audience", params=[("platforms", "vk"), ("platforms", "tg")])
        self.assertEqual(r.json(), {"recipients": 3})
        r = await self.api.post("/api/v1/broadcasts", json={"text": "x", "platforms": ["vk"], "kind": "event"})
        self.assertEqual(r.status_code, 422)
        r = await self.api.post("/api/v1/broadcasts", json={"text": "", "platforms": ["vk"], "kind": "at"})
        self.assertEqual(r.status_code, 422)
        b = await self.create(title=" Акция ", text="x", platforms=["tg", "vk", "tg"], kind="at")
        self.assertEqual((b["title"], b["platforms"], b["has_image"]), ("Акция", ["tg", "vk"], False))
        r = await self.api.patch(f"/api/v1/broadcasts/{b['id']}", json={"delay_minutes": 5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(len((await self.api.get("/api/v1/broadcasts")).json()), 1)
        self.assertEqual((await self.api.delete(f"/api/v1/broadcasts/{b['id']}")).status_code, 204)
        self.assertEqual((await self.api.get(f"/api/v1/broadcasts/{b['id']}")).status_code, 404)
        for kind, code in (("tg_bot", 403), ("app", 403), ("bot", 200)):
            self.client_kind = kind
            self.assertEqual((await self.api.get("/api/v1/broadcasts")).status_code, code, kind)


if __name__ == "__main__":
    unittest.main()
