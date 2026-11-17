"""Автозакрытие молчащих диалогов на настоящем Postgres.

Нужна пустая временная база: TEST_DATABASE_URL=postgresql://user:pass@host:port/db
Запуск из chat_api/: TEST_DATABASE_URL=... python -m pytest tests/test_inactivity_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

from app.db import create_pool, ensure_schema
from app.peer import Peer
from app.repositories import sessions as repo
from app.services.chat import ChatService
from app.services.inactivity import InactivitySweeper

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if sys.platform == "win32":
    # psycopg async не работает на ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

A = Peer("vk", "club1", 11)
B = Peer("tg", "bot", 22)
ENGINE = Peer("engine", "acc1", 1)

IDLE = 30 * 60
SILENT = 24 * 3600


class FakeEvents:
    def __init__(self) -> None:
        self.sent: list[tuple[Peer, object]] = []

    async def publish(self, peer: Peer, event: object) -> None:
        self.sent.append((peer, event))

    async def request_engine(self) -> None:
        pass


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL не задан")
class InactivityDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = create_pool(DATABASE_URL or "")
        await self.pool.open(wait=True, timeout=30)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute("TRUNCATE messages, message_archives, dialogs, chat_sessions, bot_users CASCADE")
        self.events = FakeEvents()
        settings = SimpleNamespace(
            engine_enabled=False, engine_fallback_after_seconds=0, stats_online_window_seconds=300
        )
        self.chat = ChatService(self.pool, self.events, settings)  # type: ignore[arg-type]
        self.sweeper = InactivitySweeper(self.pool, self.chat, minutes=30, silent_hours=24)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def pair(self, a: Peer, b: Peer, idle_minutes: float) -> int:
        """Диалог, где последняя активность была idle_minutes минут назад."""
        async with self.pool.connection() as conn:
            for peer in (a, b):
                if not peer.is_engine:
                    await repo.touch_user(conn, peer)
            await repo.set_searching(conn, a)
            await repo.set_searching(conn, b)
            dialog_id = await repo.open_dialog(conn, a, b)
            await repo.set_chatting(conn, a, b, dialog_id)
            await repo.set_chatting(conn, b, a, dialog_id)
            await self.age(conn, idle_minutes)
        return dialog_id

    async def age(self, conn, minutes: float) -> None:  # type: ignore[no-untyped-def]
        await conn.execute(
            "UPDATE chat_sessions SET last_activity_at = NOW() - make_interval(mins => %s)", (minutes,)
        )

    async def dialog(self, dialog_id: int) -> dict:
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM dialogs WHERE id = %s", (dialog_id,))
            return await cursor.fetchone()

    async def counter(self, peer: Peer) -> int:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT dialogs_ended FROM bot_users WHERE platform = %s AND user_id = %s",
                (peer.platform, peer.user_id),
            )
            return int((await cursor.fetchone())["dialogs_ended"])

    async def test_schema_twice_and_constraint(self) -> None:
        await ensure_schema(self.pool)
        dialog_id = await self.pair(A, B, 60)
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE dialogs SET end_reason = 'inactive', ended_at = NOW() WHERE id = %s", (dialog_id,))

    async def test_closes_after_threshold_and_notifies_both(self) -> None:
        dialog_id = await self.pair(A, B, 31)
        self.assertEqual(await self.sweeper.run_once(), 1)
        row = await self.dialog(dialog_id)
        self.assertEqual(row["end_reason"], "inactive")
        self.assertIsNone(row["ended_by"])
        self.assertEqual(await self.chat.state(A), (await self.chat.state(B)))
        self.assertEqual((await self.chat.state(A)).phase, "idle")
        left = {peer: event for peer, event in self.events.sent if event.type == "partner_left"}
        self.assertEqual(set(left), {A, B})
        self.assertTrue(all(e.reason == "inactive" and not e.promo for e in left.values()))

    async def test_active_dialog_untouched(self) -> None:
        dialog_id = await self.pair(A, B, 29)
        self.assertEqual(await self.sweeper.run_once(), 0)
        self.assertIsNone((await self.dialog(dialog_id))["ended_at"])
        self.assertEqual((await self.chat.state(A)).phase, "chatting")
        self.assertEqual(self.events.sent, [])

    async def test_message_refreshes_activity(self) -> None:
        dialog_id = await self.pair(A, B, 60)
        await self.chat.send_message(A, "привет")
        self.assertEqual(await self.sweeper.run_once(), 0)
        self.assertIsNone((await self.dialog(dialog_id))["ended_at"])

    async def test_recheck_under_lock_skips_fresh_dialog(self) -> None:
        await self.pair(A, B, 60)
        self.assertTrue(await self.chat.close_inactive(A, IDLE, SILENT))
        # Диалог закрыт; второй вызов (устаревший кандидат) ничего не делает.
        self.assertFalse(await self.chat.close_inactive(B, IDLE, SILENT))
        self.events.sent.clear()
        await self.pair(A, B, 60)
        await self.chat.send_message(A, "жив")
        self.assertFalse(await self.chat.close_inactive(A, IDLE, SILENT))
        self.assertEqual((await self.chat.state(A)).phase, "chatting")
        self.assertEqual([e.type for _, e in self.events.sent], ["message"])

    async def test_old_backlog_closed_silently(self) -> None:
        dialog_id = await self.pair(A, B, 25 * 60)
        self.assertEqual(await self.sweeper.run_once(), 1)
        self.assertEqual((await self.dialog(dialog_id))["end_reason"], "inactive")
        self.assertEqual([e for _, e in self.events.sent], [])
        self.assertEqual((await self.chat.state(B)).phase, "idle")

    async def test_legacy_null_activity_closed_silently_any_age(self) -> None:
        dialog_id = await self.pair(A, B, 0)
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET last_activity_at = NULL, updated_at = NOW() - interval '2 hours'"
            )
        self.assertEqual(await self.sweeper.run_once(), 1)
        self.assertEqual((await self.dialog(dialog_id))["end_reason"], "inactive")
        self.assertEqual(self.events.sent, [])

    async def test_legacy_engine_partner_released(self) -> None:
        await self.pair(A, ENGINE, 0)
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET last_activity_at = NULL, updated_at = NOW() - interval '2 hours'"
            )
        self.assertEqual(await self.sweeper.run_once(), 1)
        self.assertEqual((await self.chat.state(ENGINE)).phase, "idle")
        self.assertEqual([(p, e.reason) for p, e in self.events.sent], [(ENGINE, "inactive")])

    async def test_fresh_partner_keeps_pair_alive(self) -> None:
        dialog_id = await self.pair(A, B, 60)
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE chat_sessions SET last_activity_at = NOW() WHERE user_id = %s", (B.user_id,))
        self.assertFalse(await self.chat.close_inactive(A, IDLE, SILENT))
        self.assertIsNone((await self.dialog(dialog_id))["ended_at"])

    async def test_half_broken_partner_idle_or_searching(self) -> None:
        dialog_id = await self.pair(A, B, 60)
        async with self.pool.connection() as conn:
            await repo.set_searching(conn, B)
        self.events.sent.clear()
        self.assertTrue(await self.chat.close_inactive(A, IDLE, SILENT))
        # Общий диалог закрыл поиск B ("stopped"); сессия A всё ещё chatting и закрывается молча.
        self.assertIsNotNone((await self.dialog(dialog_id))["ended_at"])
        self.assertEqual((await self.chat.state(A)).phase, "idle")
        self.assertEqual((await self.chat.state(B)).phase, "searching")
        self.assertEqual(self.events.sent, [])

    async def test_half_broken_partner_talks_to_third(self) -> None:
        c = Peer("vk", "club1", 33)
        first = await self.pair(A, B, 60)
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, c)
            await repo.set_searching(conn, c)
            second = await repo.open_dialog(conn, B, c)
            await repo.set_chatting(conn, B, c, second)
            await repo.set_chatting(conn, c, B, second)
        self.events.sent.clear()
        self.assertTrue(await self.chat.close_inactive(A, IDLE, SILENT))
        self.assertEqual((await self.dialog(first))["end_reason"], "inactive")
        self.assertIsNone((await self.dialog(second))["ended_at"])
        self.assertEqual((await self.chat.state(A)).phase, "idle")
        self.assertEqual((await self.chat.state(B)).phase, "chatting")
        self.assertEqual((await self.chat.state(c)).phase, "chatting")
        self.assertEqual(self.events.sent, [])

    async def test_not_counted_for_promo(self) -> None:
        await self.pair(A, B, 40)
        await self.sweeper.run_once()
        self.assertEqual(await self.counter(A), 0)
        self.assertEqual(await self.counter(B), 0)

    async def test_stop_still_counts(self) -> None:
        await self.pair(A, B, 1)
        await self.chat.stop(A)
        self.assertEqual(await self.counter(A), 1)
        self.assertEqual(await self.counter(B), 1)

    async def test_engine_partner_released_even_when_silent(self) -> None:
        dialog_id = await self.pair(A, ENGINE, 25 * 60)
        self.assertEqual(await self.sweeper.run_once(), 1)
        self.assertEqual((await self.dialog(dialog_id))["end_reason"], "inactive")
        self.assertEqual((await self.chat.state(ENGINE)).phase, "idle")
        self.assertEqual([(p, e.type, e.reason) for p, e in self.events.sent], [(ENGINE, "partner_left", "inactive")])

    async def test_disabled_does_nothing(self) -> None:
        await self.pair(A, B, 90)
        off = InactivitySweeper(self.pool, self.chat, minutes=0, silent_hours=24)
        await off.run()
        self.assertEqual((await self.chat.state(A)).phase, "chatting")


if __name__ == "__main__":
    unittest.main()
