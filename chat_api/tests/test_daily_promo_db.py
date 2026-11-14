"""Ежедневная рассылка-промо на настоящем Postgres: TEST_DATABASE_URL или pgserver во временном каталоге."""

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.broadcasts import settings as bsettings
from app.broadcasts.service import BroadcastService
from app.db import ensure_schema
from app.events import EventBus

sys.path.insert(0, os.path.dirname(__file__))
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

MSK = timezone(timedelta(hours=3))
# 10 ноября 2026, 20:30 по Москве.
EVENING = datetime(2026, 11, 10, 20, 30, tzinfo=MSK)
OLD = datetime(2026, 11, 1, 12, 0, tzinfo=MSK)


class FakeBus:
    def __init__(self):
        self.calls = []

    async def publish_notices(self, recipients, text, *, media_id=None, content_type=None, screen=None):
        self.calls.append(([(p.platform, p.bot_key, p.user_id) for p in recipients], text, screen))

    def people(self):
        return sorted((pl, uid) for call in self.calls for pl, _, uid in call[0])


async def _no_sleep(_):
    return None


class DailyPromoDbTest(unittest.IsolatedAsyncioTestCase):
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
                "TRUNCATE daily_promo_log, daily_promos, broadcast_log, broadcasts, reminder_log, user_channels, "
                "chat_sessions, bot_users, dialogs, channels RESTART IDENTITY CASCADE"
            )
        self.now = EVENING
        self.bus = FakeBus()
        self.rand_value = 0.0

    def service(self, bus=None, enabled=True):
        return BroadcastService(
            self.pool,
            bus or self.bus,
            clock=lambda: self.now,
            sleep=_no_sleep,
            daily_promo=enabled,
            rand=lambda: self.rand_value,
        )

    async def sql(self, query, args=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, args)
            return await cur.fetchall() if cur.description else None

    async def channel(self, platform="vk", external_id="-100", enabled=True):
        rows = await self.sql(
            "INSERT INTO channels (platform, external_id, enabled) VALUES (%s, %s, %s) RETURNING id",
            (platform, external_id, enabled),
        )
        return rows[0]["id"]

    async def link(self, channel_id, user_id, platform="vk", started=OLD, seen=OLD):
        await self.sql(
            "INSERT INTO bot_users (platform, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (platform, user_id)
        )
        await self.sql(
            "INSERT INTO user_channels (user_platform, user_id, channel_id, started_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (platform, user_id, channel_id, started, seen),
        )

    async def phase(self, user_id, phase, platform="vk", bot_key="-100"):
        await self.sql(
            "INSERT INTO chat_sessions (platform, bot_key, user_id, phase) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (platform, bot_key, user_id) DO UPDATE SET phase = EXCLUDED.phase",
            (platform, bot_key, user_id, phase),
        )

    async def day_row(self):
        return (await self.sql("SELECT day, send_at, sent_count FROM daily_promos ORDER BY day"))

    # ---------- проверки ----------

    async def test_disabled_does_nothing(self):
        await self.link(await self.channel(), 1)
        self.assertEqual(await self.service(enabled=False).run_once(), 0)
        self.assertEqual(self.bus.calls, [])
        self.assertEqual(await self.day_row(), [])

    async def test_before_send_at_sends_nothing(self):
        await self.link(await self.channel(), 1)
        self.rand_value = 0.9  # ~20:48 МСК
        self.now = datetime(2026, 11, 10, 19, 30, tzinfo=MSK)
        self.assertEqual(await self.service().run_once(), 0)
        self.assertEqual(self.bus.calls, [])
        self.assertEqual(len(await self.day_row()), 1)

    async def test_sends_one_notice_per_person_with_screen(self):
        vk = await self.channel("vk", "-100")
        vk_off = await self.channel("vk", "-200", enabled=False)
        tg = await self.channel("tg", "bot")
        web = await self.channel("web", "site")
        await self.link(vk, 1)
        await self.link(tg, 2, platform="tg")
        await self.link(vk_off, 3)
        await self.link(web, 4, platform="web")
        self.assertEqual(await self.service().run_once(), 2)
        self.assertEqual(self.bus.people(), [("tg", 2), ("vk", 1)])
        expected_screen, expected_text = bsettings.daily_promo_for(EVENING.date())
        for _, text, screen in self.bus.calls:
            self.assertEqual(screen, expected_screen)
            self.assertEqual(text, expected_text)
        self.assertEqual((await self.day_row())[0]["sent_count"], 2)

    async def test_person_in_two_communities_gets_one_via_latest_seen(self):
        old = await self.channel("vk", "-100")
        fresh = await self.channel("vk", "-300")
        await self.link(old, 1, seen=OLD)
        await self.link(fresh, 1, seen=OLD + timedelta(days=3))
        self.assertEqual(await self.service().run_once(), 1)
        self.assertEqual(self.bus.calls[0][0], [("vk", "-300", 1)])

    async def test_no_duplicates_on_repeat_and_new_instance(self):
        await self.link(await self.channel(), 1)
        self.rand_value = 0.5
        self.assertEqual(await self.service().run_once(), 1)
        send_at = (await self.day_row())[0]["send_at"]
        self.assertEqual(await self.service().run_once(), 0)
        self.rand_value = 0.1  # другой экземпляр выбрал бы другое время, но день уже зафиксирован
        self.assertEqual(await self.service(bus=FakeBus()).run_once(), 0)
        rows = await self.day_row()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["send_at"], send_at)
        self.assertEqual(rows[0]["sent_count"], 1)
        self.assertEqual(len(self.bus.people()), 1)

    async def test_busy_person_skipped_then_sent_after_idle(self):
        channel = await self.channel()
        await self.link(channel, 1)
        await self.link(channel, 2)
        await self.link(channel, 3)
        await self.phase(1, "searching")
        await self.phase(2, "chatting")
        self.assertEqual(await self.service().run_once(), 1)
        self.assertEqual(self.bus.people(), [("vk", 3)])
        await self.phase(1, "idle")
        await self.phase(2, "idle")
        self.assertEqual(await self.service().run_once(), 2)
        self.assertEqual(self.bus.people(), [("vk", 1), ("vk", 2), ("vk", 3)])
        self.assertEqual(await self.service().run_once(), 0)

    async def test_after_23_msk_sends_nothing(self):
        await self.link(await self.channel(), 1)
        self.now = datetime(2026, 11, 10, 23, 5, tzinfo=MSK)
        self.assertEqual(await self.service().run_once(), 0)
        self.assertEqual(self.bus.calls, [])

    async def test_link_created_after_send_at_is_skipped(self):
        channel = await self.channel()
        self.rand_value = 0.0  # 19:00 МСК
        self.now = datetime(2026, 11, 10, 19, 30, tzinfo=MSK)
        await self.link(channel, 1)
        await self.link(channel, 2, started=datetime(2026, 11, 10, 19, 10, tzinfo=MSK))
        self.assertEqual(await self.service().run_once(), 1)
        self.assertEqual(self.bus.people(), [("vk", 1)])

    async def test_send_at_stays_in_window(self):
        for value, day in ((0.0, 10), (0.999999, 11), (1.0, 12)):
            self.rand_value = value
            self.now = datetime(2026, 11, day, 12, 0, tzinfo=MSK)
            await self.service().run_once()
        rows = await self.day_row()
        self.assertEqual(len(rows), 3)
        for row in rows:
            local = row["send_at"].astimezone(MSK)
            start = datetime(local.year, local.month, local.day, 19, tzinfo=MSK)
            self.assertGreaterEqual(local, start)
            self.assertLess(local, start + timedelta(hours=2))
        self.assertEqual(rows[0]["send_at"], datetime(2026, 11, 10, 19, tzinfo=MSK))

    async def test_next_day_is_new_row_and_sends_again(self):
        await self.link(await self.channel(), 1)
        self.assertEqual(await self.service().run_once(), 1)
        self.now = EVENING + timedelta(days=1)
        self.assertEqual(await self.service().run_once(), 1)
        self.assertEqual(len(await self.day_row()), 2)
        self.assertEqual(len(self.bus.people()), 2)

    async def test_screen_alternates_by_msk_day(self):
        await self.link(await self.channel(), 1)
        for day, screen in ((14, "daily_hello"), (15, "daily_promo"), (16, "daily_hello"), (13, "daily_promo")):
            self.bus.calls.clear()
            self.now = datetime(2026, 11, day, 20, 30, tzinfo=MSK)
            self.assertEqual(await self.service().run_once(), 1, day)
            self.assertEqual([c[2] for c in self.bus.calls], [screen], day)
            self.assertEqual(self.bus.calls[0][1], dict(bsettings.DAILY_PROMO_ROTATION)[screen])

    async def test_failed_batch_is_rolled_back_and_retried(self):
        await self.link(await self.channel(), 1)

        class Broken(FakeBus):
            async def publish_notices(self, *args, **kwargs):
                raise RuntimeError("redis down")

        self.assertEqual(await self.service(bus=Broken()).run_once(), 0)
        self.assertEqual(await self.sql("SELECT 1 FROM daily_promo_log"), [])
        self.assertEqual(await self.service().run_once(), 1)


class FakeRedis:
    def __init__(self):
        self.added = []

    async def xadd(self, stream, fields, maxlen=None, approximate=None):
        self.added.append((stream, json.loads(fields["data"])))


class PublishNoticesScreenTest(unittest.IsolatedAsyncioTestCase):
    async def test_screen_in_event_only_when_given(self):
        from app.peer import Peer

        redis = FakeRedis()
        bus = EventBus(redis, 100)
        await bus.publish_notices([Peer("vk", "-100", 1)], "текст", screen="daily_promo")
        await bus.publish_notices([Peer("tg", "bot", 2)], "текст")
        (stream1, body1), (stream2, body2) = redis.added
        self.assertEqual(stream1, "chat:broadcast:vk")
        self.assertEqual(body1["screen"], "daily_promo")
        self.assertEqual(body1["type"], "notice")
        self.assertEqual(stream2, "chat:broadcast:tg")
        self.assertNotIn("screen", body2)


if __name__ == "__main__":
    unittest.main()
