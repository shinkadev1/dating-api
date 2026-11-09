"""История диалогов на настоящем Postgres: схема, открытие и закрытие диалога, выгрузка.

Нужна пустая временная база: TEST_DATABASE_URL=postgresql://user:pass@host:port/db
Запуск из chat_api/: TEST_DATABASE_URL=... python -m pytest tests/test_dialog_history_db.py
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, date, datetime, timedelta

from app.db import create_pool, ensure_schema
from app.peer import Peer
from app.repositories import messages as messages_repo
from app.repositories import sessions as repo

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

A = Peer("vk", "club1", 11)
B = Peer("tg", "bot", 22)


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL не задан")
class DialogHistoryDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = create_pool(DATABASE_URL or "")
        await self.pool.open(wait=True, timeout=30)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE messages, message_archives, dialogs, chat_sessions RESTART IDENTITY"
            )

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def dialog(self, conn) -> int:  # type: ignore[no-untyped-def]
        """Пара A (инициатор) и B в chatting с общим диалогом."""
        await repo.set_searching(conn, A)
        await repo.set_searching(conn, B)
        dialog_id = await repo.open_dialog(conn, A, B)
        await repo.set_chatting(conn, A, B, dialog_id)
        await repo.set_chatting(conn, B, A, dialog_id)
        return dialog_id

    async def one(self, conn, sql: str, *args):  # type: ignore[no-untyped-def]
        cursor = await conn.execute(sql, args)
        return await cursor.fetchone()

    async def test_schema_twice_and_roles(self) -> None:
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            row = await self.one(
                conn,
                "SELECT count(*) AS n FROM information_schema.tables "
                "WHERE table_name IN ('dialogs', 'messages', 'message_archives')",
            )
            self.assertEqual(row["n"], 3)
            row = await self.one(
                conn,
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_name = 'chat_sessions' AND column_name = 'dialog_id'",
            )
            self.assertEqual(row["n"], 1)

    async def test_open_chat_message_close_flow(self) -> None:
        async with self.pool.connection() as conn:
            dialog_id = await self.dialog(conn)
            for peer in (A, B):
                session = await repo.get_session(conn, peer)
                self.assertEqual(session.dialog_id, dialog_id)
            self.assertTrue(
                await messages_repo.insert_message(
                    conn, dialog_id=dialog_id, sender=A, partner=B, kind="text", status="delivered", text="привет"
                )
            )
            self.assertTrue(
                await messages_repo.insert_message(
                    conn, dialog_id=dialog_id, sender=B, partner=None, kind="audio", status="skipped", tg_file_id="F"
                )
            )
            rows = await (await conn.execute("SELECT sender, kind, status FROM messages ORDER BY id")).fetchall()
            self.assertEqual([(r["sender"], r["kind"], r["status"]) for r in rows], [("a", "text", "delivered"), ("b", "audio", "skipped")])

            await repo.set_idle(conn, A, reason="reported", ended_by=A)
            row = await self.one(conn, "SELECT ended_at, end_reason, ended_by FROM dialogs WHERE id = %s", dialog_id)
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual((row["end_reason"], row["ended_by"]), ("reported", "a"))

            # Повторное закрытие со стороны B ничего не перезаписывает.
            await repo.set_idle(conn, B, reason="stopped", ended_by=B)
            row = await self.one(conn, "SELECT end_reason, ended_by FROM dialogs WHERE id = %s", dialog_id)
            self.assertEqual((row["end_reason"], row["ended_by"]), ("reported", "a"))
            self.assertIsNone((await repo.get_session(conn, B)).dialog_id)

    async def test_ended_by_b(self) -> None:
        async with self.pool.connection() as conn:
            dialog_id = await self.dialog(conn)
            await repo.set_idle(conn, B, reason="stopped", ended_by=B)
            row = await self.one(conn, "SELECT end_reason, ended_by FROM dialogs WHERE id = %s", dialog_id)
            self.assertEqual((row["end_reason"], row["ended_by"]), ("stopped", "b"))

    async def test_set_searching_closes_by_service(self) -> None:
        async with self.pool.connection() as conn:
            dialog_id = await self.dialog(conn)
            await repo.set_searching(conn, A)
            row = await self.one(conn, "SELECT ended_at, end_reason, ended_by FROM dialogs WHERE id = %s", dialog_id)
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual((row["end_reason"], row["ended_by"]), ("stopped", None))
            self.assertIsNone((await repo.get_session(conn, A)).dialog_id)

    async def test_foreign_dialog_id_is_rejected(self) -> None:
        stranger = Peer("vk", "club1", 99)
        async with self.pool.connection() as conn:
            dialog_id = await self.dialog(conn)
            self.assertFalse(
                await messages_repo.insert_message(
                    conn, dialog_id=dialog_id, sender=stranger, partner=None, kind="text", status="delivered", text="x"
                )
            )
            self.assertFalse(
                await messages_repo.insert_message(
                    conn, dialog_id=dialog_id, sender=A, partner=stranger, kind="text", status="delivered", text="x"
                )
            )
            row = await self.one(conn, "SELECT count(*) AS n FROM messages")
            self.assertEqual(row["n"], 0)

    async def seed_old(self, conn, count: int) -> tuple[date, list[int]]:  # type: ignore[no-untyped-def]
        dialog_id = await self.dialog(conn)
        old = datetime.now(UTC) - timedelta(days=120)
        for i in range(count):
            await conn.execute(
                "INSERT INTO messages (dialog_id, sender, created_at, kind, text, status) "
                "VALUES (%s, 'a', %s, 'text', %s, 'delivered')",
                (dialog_id, old + timedelta(seconds=i), f"t{i}"),
            )
        await messages_repo.insert_message(
            conn, dialog_id=dialog_id, sender=B, partner=A, kind="text", status="delivered", text="свежая"
        )
        rows = await messages_repo.day_rows(
            conn,
            datetime(old.year, old.month, old.day, tzinfo=UTC),
            datetime(old.year, old.month, old.day, tzinfo=UTC) + timedelta(days=1),
            100,
        )
        return old.date(), [int(r["id"]) for r in rows]

    async def test_commit_archive_deletes_exact_ids(self) -> None:
        async with self.pool.connection() as conn:
            day, ids = await self.seed_old(conn, 3)
            self.assertEqual(len(ids), 3)
            self.assertEqual(await messages_repo.next_part(conn, day), 1)
            await messages_repo.commit_archive(
                conn, day=day, part=1, key="k1", rows_count=len(ids), size_bytes=10, ids=ids
            )
            left = await (await conn.execute("SELECT text FROM messages")).fetchall()
            self.assertEqual([r["text"] for r in left], ["свежая"])
            row = await self.one(conn, "SELECT rows_count, first_message_id, last_message_id FROM message_archives")
            self.assertEqual((row["rows_count"], row["first_message_id"], row["last_message_id"]), (len(ids), min(ids), max(ids)))
            self.assertEqual(await messages_repo.next_part(conn, day), 2)

    async def test_commit_archive_duplicate_part_rolls_back(self) -> None:
        async with self.pool.connection() as conn:
            day, ids = await self.seed_old(conn, 4)
            await messages_repo.commit_archive(
                conn, day=day, part=1, key="k1", rows_count=2, size_bytes=10, ids=ids[:2]
            )
            before = await self.one(conn, "SELECT count(*) AS n FROM messages")
            with self.assertRaises(Exception):
                await messages_repo.commit_archive(
                    conn, day=day, part=1, key="k2", rows_count=2, size_bytes=10, ids=ids[2:]
                )
            after = await self.one(conn, "SELECT count(*) AS n FROM messages")
            self.assertEqual(before["n"], after["n"])
            row = await self.one(conn, "SELECT count(*) AS n FROM message_archives")
            self.assertEqual(row["n"], 1)

    async def test_commit_archive_missing_ids_rolls_back(self) -> None:
        async with self.pool.connection() as conn:
            day, ids = await self.seed_old(conn, 2)
            with self.assertRaises(RuntimeError):
                await messages_repo.commit_archive(
                    conn, day=day, part=1, key="k1", rows_count=len(ids) + 1, size_bytes=10, ids=[*ids, 10**9]
                )
            row = await self.one(conn, "SELECT count(*) AS n FROM messages")
            self.assertEqual(row["n"], len(ids) + 1)
            row = await self.one(conn, "SELECT count(*) AS n FROM message_archives")
            self.assertEqual(row["n"], 0)


if __name__ == "__main__":
    unittest.main()
