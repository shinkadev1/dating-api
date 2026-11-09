"""Архиватор реплик без базы и S3: выгрузка, проверка загрузки и условия, при которых ничего не удаляется.

Запуск из chat_api/: python -m pytest tests
"""

from __future__ import annotations

import gzip
import json
import logging
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import patch

from app.services import archive as archive_module
from app.services import storage
from app.services.archive import MessageArchiver, archive_key
from app.services.storage import StorageError

TODAY = date(2026, 11, 10)
DAY = date(2026, 7, 1)


def message_row(message_id: int) -> dict:
    return {
        "id": message_id,
        "dialog_id": 5,
        "sender": "a",
        "created_at": datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        "kind": "text",
        "text": "привет",
        "media_id": None,
        "tg_file_id": None,
        "tg_method": None,
        "content_type": None,
        "status": "delivered",
        "a_platform": "vk",
        "a_bot_key": "club1",
        "a_user_id": 11,
        "b_platform": "tg",
        "b_bot_key": "bot",
        "b_user_id": 22,
        "started_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        "ended_at": None,
        "end_reason": None,
        "ended_by": None,
    }


class FakeConn:
    closed = False

    async def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn()

    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield self.conn


class FakeStorage:
    def __init__(self) -> None:
        self.enabled = True
        self.objects: dict[str, bytes] = {}
        self.put_error = False
        self.size_delta = 0
        self.get_override: bytes | None = None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        if self.put_error:
            raise StorageError(key)
        self.objects[key] = body

    async def head(self, key: str) -> int:
        return len(self.objects[key]) + self.size_delta

    async def get(self, key: str) -> bytes:
        return self.get_override if self.get_override is not None else self.objects[key]


class FakeRepo:
    def __init__(self, rows: list[dict], part: int = 1, locked: bool = True) -> None:
        self.rows = rows
        self.part = part
        self.locked = locked
        self.unlocked = 0
        self.commits: list[dict] = []

    async def try_archive_lock(self, conn: object) -> bool:
        return self.locked

    async def archive_unlock(self, conn: object) -> None:
        self.unlocked += 1

    async def oldest_message_at(self, conn: object):  # type: ignore[no-untyped-def]
        return self.rows[0]["created_at"] if self.rows else None

    async def day_rows(self, conn: object, start: datetime, end: datetime, limit: int) -> list[dict]:
        return self.rows[:limit]

    async def next_part(self, conn: object, day: date) -> int:
        return self.part

    async def commit_archive(self, conn: object, **kwargs: object) -> None:
        self.commits.append(kwargs)
        ids = set(kwargs["ids"])  # type: ignore[arg-type]
        self.rows = [row for row in self.rows if row["id"] not in ids]


def archiver(repo: FakeRepo, store: FakeStorage, **kwargs: int) -> MessageArchiver:
    return MessageArchiver(FakePool(), store, hot_days=kwargs.pop("hot_days", 90), **kwargs)  # type: ignore[arg-type]


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def run_once(self, repo: FakeRepo, store: FakeStorage, **kwargs: int) -> int:
        with patch.object(archive_module, "repo", repo):
            return await archiver(repo, store, **kwargs).run_once(today=TODAY)

    def test_keys(self) -> None:
        self.assertEqual(archive_key(DAY, 1), "archive/messages/2026/07/01.jsonl.gz")
        self.assertEqual(archive_key(DAY, 2), "archive/messages/2026/07/01.part2.jsonl.gz")

    async def test_success_uploads_verifies_and_commits_exact_ids(self) -> None:
        repo = FakeRepo([message_row(1), message_row(2), message_row(3)])
        store = FakeStorage()
        with self.assertLogs(archive_module.logger, level=logging.INFO) as logs:
            parts = await self.run_once(repo, store)
        self.assertEqual(parts, 1)
        key = "archive/messages/2026/07/01.jsonl.gz"
        lines = gzip.decompress(store.objects[key]).decode("utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        first = json.loads(lines[0])
        self.assertEqual((first["id"], first["text"], first["a_platform"], first["b_user_id"]), (1, "привет", "vk", 22))
        self.assertEqual(len(repo.commits), 1)
        commit = repo.commits[0]
        self.assertEqual(
            (commit["day"], commit["part"], commit["key"], commit["rows_count"], commit["ids"]),
            (DAY, 1, key, 3, [1, 2, 3]),
        )
        self.assertEqual(commit["size_bytes"], len(store.objects[key]))
        self.assertTrue(any(f"Архив сообщений: {key} — 3 строк" in line for line in logs.output))
        self.assertEqual(repo.unlocked, 1)
        self.assertNotIn("привет", "\n".join(logs.output))

    async def test_put_error_deletes_nothing(self) -> None:
        repo = FakeRepo([message_row(1)])
        store = FakeStorage()
        store.put_error = True
        with self.assertLogs(archive_module.logger, level=logging.ERROR):
            parts = await self.run_once(repo, store)
        self.assertEqual(parts, 0)
        self.assertEqual(repo.commits, [])
        self.assertEqual(repo.unlocked, 1)

    async def test_head_size_mismatch_deletes_nothing(self) -> None:
        repo = FakeRepo([message_row(1)])
        store = FakeStorage()
        store.size_delta = 1
        with self.assertLogs(archive_module.logger, level=logging.ERROR):
            await self.run_once(repo, store)
        self.assertEqual(repo.commits, [])

    async def test_reread_with_other_row_count_deletes_nothing(self) -> None:
        repo = FakeRepo([message_row(1), message_row(2)])
        store = FakeStorage()
        store.get_override = gzip.compress((json.dumps(message_row(1), default=str) + "\n").encode("utf-8"))
        with self.assertLogs(archive_module.logger, level=logging.ERROR):
            await self.run_once(repo, store)
        self.assertEqual(repo.commits, [])

    async def test_storage_disabled_warns_and_skips(self) -> None:
        repo = FakeRepo([message_row(1)])
        store = FakeStorage()
        store.enabled = False
        with self.assertLogs(archive_module.logger, level=logging.WARNING) as logs:
            parts = await self.run_once(repo, store)
        self.assertEqual(parts, 0)
        self.assertIn("S3 не настроен: архив сообщений пропущен", "\n".join(logs.output))
        self.assertEqual(repo.commits, [])
        self.assertEqual(store.objects, {})

    async def test_unlock_failure_closes_connection(self) -> None:
        repo = FakeRepo([message_row(1)])

        async def broken_unlock(conn: object) -> None:
            raise RuntimeError("обрыв")

        repo.archive_unlock = broken_unlock  # type: ignore[method-assign]
        pool = FakePool()
        with patch.object(archive_module, "repo", repo), self.assertLogs(archive_module.logger, level=logging.ERROR) as logs:
            await MessageArchiver(pool, FakeStorage(), hot_days=90).run_once(today=TODAY)  # type: ignore[arg-type]
        self.assertTrue(pool.conn.closed)
        self.assertIn("не снял блокировку", "\n".join(logs.output))

    async def test_lock_not_taken_does_nothing(self) -> None:
        repo = FakeRepo([message_row(1)], locked=False)
        store = FakeStorage()
        with self.assertLogs(archive_module.logger, level=logging.INFO):
            parts = await self.run_once(repo, store)
        self.assertEqual(parts, 0)
        self.assertEqual(store.objects, {})
        self.assertEqual(repo.unlocked, 0)

    async def test_second_part_gets_part_suffix(self) -> None:
        repo = FakeRepo([message_row(1)], part=2)
        store = FakeStorage()
        await self.run_once(repo, store)
        self.assertEqual(list(store.objects), ["archive/messages/2026/07/01.part2.jsonl.gz"])
        self.assertEqual(repo.commits[0]["part"], 2)

    async def test_recent_day_is_not_archived(self) -> None:
        # Граница: 2026-11-10 минус 90 дней = 2026-08-12; день на границе остаётся в базе.
        row = message_row(1)
        row["created_at"] = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        repo = FakeRepo([row])
        store = FakeStorage()
        with self.assertLogs(archive_module.logger, level=logging.INFO) as logs:
            parts = await self.run_once(repo, store)
        self.assertEqual(parts, 0)
        self.assertEqual(repo.commits, [])
        self.assertIn("выгружать нечего", "\n".join(logs.output))

    async def test_batches_by_batch_rows_into_parts(self) -> None:
        repo = FakeRepo([message_row(i) for i in range(1, 6)])
        store = FakeStorage()

        async def next_part(conn: object, day: date) -> int:
            return len(repo.commits) + 1

        repo.next_part = next_part  # type: ignore[method-assign]
        parts = await self.run_once(repo, store, batch_rows=2)
        self.assertEqual(parts, 3)
        self.assertEqual([c["ids"] for c in repo.commits], [[1, 2], [3, 4], [5]])
        self.assertEqual(repo.rows, [])


class FakeS3Client:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error

    async def head_object(self, **_: object) -> dict:
        if self.error:
            raise self.error
        return self.response or {}


class StorageHeadTests(unittest.IsolatedAsyncioTestCase):
    def make(self, client: FakeS3Client | None) -> storage.ObjectStorage:
        s = storage.ObjectStorage(endpoint="https://s3.example", region="ru1", bucket="b", access_key="a", secret_key="s")
        s._client = client  # type: ignore[attr-defined]
        return s

    async def test_returns_content_length(self) -> None:
        self.assertEqual(await self.make(FakeS3Client({"ContentLength": 321})).head("k"), 321)

    async def test_error_becomes_storage_error(self) -> None:
        with self.assertRaises(StorageError), self.assertLogs(storage.logger, level=logging.ERROR):
            await self.make(FakeS3Client(error=RuntimeError("нет объекта"))).head("k")

    async def test_disabled_storage_raises(self) -> None:
        with self.assertRaises(StorageError):
            await self.make(None).head("k")


if __name__ == "__main__":
    unittest.main()
