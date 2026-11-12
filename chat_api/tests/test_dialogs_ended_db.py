"""Счётчик завершённых диалогов bot_users.dialogs_ended на настоящем Postgres.

Нужна пустая временная база: TEST_DATABASE_URL=postgresql://user:pass@host:port/db
Запуск из chat_api/: TEST_DATABASE_URL=... python -m pytest tests/test_dialogs_ended_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

from app.db import create_pool, ensure_schema
from app.peer import Peer
from app.repositories import sessions as repo

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if sys.platform == "win32":
    # psycopg async не работает на ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

A = Peer("vk", "club1", 11)
B = Peer("tg", "bot", 22)
ENGINE = Peer("engine", "acc1", 1)


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL не задан")
class DialogsEndedDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = create_pool(DATABASE_URL or "")
        await self.pool.open(wait=True, timeout=30)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute("TRUNCATE messages, message_archives, dialogs, chat_sessions, bot_users CASCADE")

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def counter(self, conn, peer: Peer) -> int | None:  # type: ignore[no-untyped-def]
        cursor = await conn.execute(
            "SELECT dialogs_ended FROM bot_users WHERE platform = %s AND user_id = %s",
            (peer.platform, peer.user_id),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row["dialogs_ended"])

    async def pair(self, conn, a: Peer, b: Peer) -> int:  # type: ignore[no-untyped-def]
        await repo.set_searching(conn, a)
        await repo.set_searching(conn, b)
        dialog_id = await repo.open_dialog(conn, a, b)
        await repo.set_chatting(conn, a, b, dialog_id)
        await repo.set_chatting(conn, b, a, dialog_id)
        return dialog_id

    async def test_schema_twice_and_column(self) -> None:
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'bot_users' AND column_name = 'dialogs_ended'"
            )
            self.assertEqual((await cursor.fetchone())["n"], 1)

    async def test_each_side_counted_exactly_once(self) -> None:
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, A)
            await repo.touch_user(conn, B)
            await self.pair(conn, A, B)
            first = await repo.set_idle(conn, A, reason="stopped", ended_by=A)
            self.assertEqual({(e.peer, e.dialogs_ended) for e in first}, {(A, 1), (B, 1)})
            second = await repo.set_idle(conn, B, reason="stopped", ended_by=A)
            self.assertEqual(second, [])
            self.assertEqual(await self.counter(conn, A), 1)
            self.assertEqual(await self.counter(conn, B), 1)

    async def test_repeated_set_searching_does_not_increment(self) -> None:
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, A)
            await repo.touch_user(conn, B)
            await self.pair(conn, A, B)
            ended = await repo.set_searching(conn, A)
            self.assertEqual(len(ended), 2)
            self.assertEqual(await repo.set_searching(conn, A), [])
            self.assertEqual(await repo.set_idle(conn, B, reason="stopped", ended_by=A), [])
            self.assertEqual(await self.counter(conn, A), 1)
            self.assertEqual(await self.counter(conn, B), 1)

    async def test_counter_accumulates_over_dialogs(self) -> None:
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, A)
            await repo.touch_user(conn, B)
            for _ in range(3):
                await self.pair(conn, A, B)
                await repo.set_idle(conn, A, reason="stopped", ended_by=A)
            self.assertEqual(await self.counter(conn, A), 3)
            self.assertEqual(await self.counter(conn, B), 3)

    async def test_engine_dialog_counts_only_human(self) -> None:
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, A)
            await self.pair(conn, ENGINE, A)
            ended = await repo.set_idle(conn, A, reason="stopped", ended_by=A)
            self.assertEqual([(e.peer, e.dialogs_ended) for e in ended], [(A, 1)])
            self.assertEqual(await self.counter(conn, A), 1)
            self.assertIsNone(await self.counter(conn, ENGINE))

    async def test_missing_bot_users_row_is_skipped(self) -> None:
        async with self.pool.connection() as conn:
            await repo.touch_user(conn, A)
            await self.pair(conn, A, B)
            ended = await repo.set_idle(conn, A, reason="stopped", ended_by=A)
            self.assertEqual([(e.peer, e.dialogs_ended) for e in ended], [(A, 1)])
            self.assertIsNone(await self.counter(conn, B))


if __name__ == "__main__":
    unittest.main()
