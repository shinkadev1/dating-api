"""Напоминания: тихие часы, окно выборки, проход с подставными базой, Redis и S3."""

import asyncio
import contextlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.media_types import MediaRejected
from app.peer import Peer
from app.reminders import content, settings
from app.reminders import service as svc
from app.services.media import StorageDisabled

# 12:00 UTC = 15:00 МСК, вне тихих часов.
DAY = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
QUIET = (23, 10)


def utc_for_msk(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 10, hour, minute, tzinfo=timezone.utc) - timedelta(hours=3)


class IsQuietTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(svc.is_quiet(utc_for_msk(22, 59), QUIET))
        self.assertTrue(svc.is_quiet(utc_for_msk(23, 0), QUIET))
        self.assertTrue(svc.is_quiet(utc_for_msk(3, 0), QUIET))
        self.assertTrue(svc.is_quiet(utc_for_msk(9, 59), QUIET))
        self.assertFalse(svc.is_quiet(utc_for_msk(10, 0), QUIET))

    def test_same_day_interval_and_empty(self):
        self.assertTrue(svc.is_quiet(utc_for_msk(13, 0), (12, 14)))
        self.assertFalse(svc.is_quiet(utc_for_msk(14, 0), (12, 14)))
        self.assertFalse(svc.is_quiet(utc_for_msk(5, 0), (5, 5)))


class PureTest(unittest.TestCase):
    def test_window(self):
        before, after, logged = svc.window(DAY)
        self.assertEqual(before, DAY - timedelta(hours=settings.DELAY_HOURS))
        self.assertEqual(after, DAY - timedelta(days=settings.MAX_INACTIVE_DAYS))
        self.assertEqual(logged, DAY - timedelta(hours=settings.MIN_INTERVAL_HOURS))

    def test_valid_text(self):
        self.assertTrue(svc.valid_text("Привет"))
        self.assertTrue(svc.valid_text("а" * 4096))
        self.assertFalse(svc.valid_text("а" * 4097))
        self.assertFalse(svc.valid_text(""))
        self.assertFalse(svc.valid_text("   "))

    def test_defaults_are_safe(self):
        self.assertIs(settings.ENABLED, False)
        self.assertTrue(svc.valid_text(content.TEXT))


class Cursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class Conn:
    def __init__(self, lock_ok=True, log=None):
        self.lock_ok = lock_ok
        self.log = log if log is not None else []

    async def execute(self, query, params=None):
        if "pg_try_advisory_lock" in query:
            self.log.append("lock")
            return Cursor({"ok": self.lock_ok})
        if "pg_advisory_unlock" in query:
            self.log.append("unlock")
            return Cursor({"ok": True})
        raise AssertionError(query)

    @contextlib.asynccontextmanager
    async def transaction(self):
        self.log.append("begin")
        try:
            yield
        except BaseException:
            self.log.append("rollback")
            raise
        self.log.append("commit")


class Pool:
    def __init__(self, conn):
        self.conn = conn
        self.opened = 0

    @contextlib.asynccontextmanager
    async def connection(self):
        self.opened += 1
        yield self.conn


class Repo:
    def __init__(self, batches=(), table=True, table_error=None, log=None):
        self.batches = list(batches)
        self.table = table
        self.table_error = table_error
        self.log = log if log is not None else []
        self.calls = []

    async def table_exists(self, conn):
        self.log.append("table_exists")
        if self.table_error:
            raise self.table_error
        return self.table

    async def claim_batch(self, conn, **kwargs):
        self.calls.append(kwargs)
        self.log.append("claim")
        return self.batches.pop(0) if self.batches else []


class Events:
    def __init__(self, error=None, log=None):
        self.error = error
        self.log = log if log is not None else []
        self.calls = []

    async def publish_notices(self, recipients, text, *, media_id=None, content_type=None):
        self.log.append("publish")
        if self.error:
            raise self.error
        self.calls.append((list(recipients), text, media_id, content_type))


class Media:
    def __init__(self, error=None):
        self.error = error
        self.saved = []

    async def save(self, peer, kind, body):
        if self.error:
            raise self.error
        self.saved.append((peer, kind, body))
        return SimpleNamespace(id="11111111-1111-1111-1111-111111111111", content_type="image/jpeg")


def peers(n, start=1):
    return [Peer("vk", "-100", start + i) for i in range(n)]


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = []
        self.sleeps = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = Path(self.tmp.name) / "photo.jpg"  # по умолчанию файла нет
        self.now = DAY
        for name, value in (
            ("ENABLED", True),
            ("PHOTO_PATH", self.photo),
            ("BATCH_PAUSE_SECONDS", 0.5),
        ):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def sleep(self, seconds):
        self.log.append("sleep")
        self.sleeps.append(seconds)

    def make(self, *, batches=(), lock_ok=True, publish_error=None, media=None, table=True,
             table_error=None):
        self.conn = Conn(lock_ok, self.log)
        self.pool = Pool(self.conn)
        self.repo = Repo(batches, table, table_error, self.log)
        self.events = Events(publish_error, self.log)
        self.media = media or Media()
        return svc.ReminderService(
            self.pool, self.events, self.media,
            repo=self.repo, clock=lambda: self.now, sleep=self.sleep,
        )

    async def test_disabled_calls_nothing_and_logs_once(self):
        service = self.make()
        with patch.object(settings, "ENABLED", False):
            with self.assertLogs("app.reminders.service", "INFO") as logs:
                await service.run()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Напоминания выключены (ENABLED=False), журнал reminder_log: есть",
                      logs.output[0])
        self.assertEqual(self.log, ["table_exists"])
        self.assertEqual(self.events.calls, [])
        self.assertEqual(self.sleeps, [])

    async def test_disabled_table_missing_and_error_do_not_crash(self):
        with patch.object(settings, "ENABLED", False):
            service = self.make(table=False)
            with self.assertLogs("app.reminders.service", "INFO") as logs:
                await service.run()
            self.assertIn("журнал reminder_log: нет", logs.output[0])
            service = self.make(table_error=RuntimeError("db down"))
            with self.assertLogs("app.reminders.service", "INFO") as logs:
                await service.run()
            self.assertEqual(len(logs.records), 2)
            self.assertEqual(logs.records[0].levelname, "WARNING")
            self.assertIn("журнал reminder_log: нет", logs.output[1])

    async def test_quiet_hours(self):
        self.now = utc_for_msk(2)
        service = self.make(batches=[peers(2)])
        self.assertEqual(await service.run_once(), 0)
        self.assertEqual(self.log, [])
        self.assertEqual(self.pool.opened, 0)

    async def test_invalid_text(self):
        service = self.make(batches=[peers(2)])
        with patch.object(content, "TEXT", " "):
            with self.assertLogs("app.reminders.service", "ERROR"):
                self.assertEqual(await service.run_once(), 0)
        self.assertEqual(self.log, [])

    async def test_lock_not_acquired(self):
        service = self.make(batches=[peers(2)], lock_ok=False)
        with self.assertLogs("app.reminders.service", "INFO"):
            self.assertEqual(await service.run_once(), 0)
        self.assertEqual(self.log, ["lock"])

    async def test_two_batches_then_empty(self):
        service = self.make(batches=[peers(2), peers(1, 10)])
        self.assertEqual(await service.run_once(), 3)
        self.assertEqual(
            self.log,
            ["lock", "begin", "claim", "publish", "commit", "sleep",
             "begin", "claim", "publish", "commit", "sleep",
             "begin", "claim", "commit", "unlock"],
        )
        self.assertEqual(len(self.events.calls), 2)
        self.assertEqual(self.events.calls[0][1], content.TEXT)
        self.assertEqual(self.sleeps, [0.5, 0.5])
        before, after, logged = svc.window(DAY)
        first = self.repo.calls[0]
        self.assertEqual(first["platforms"], list(settings.PLATFORMS))
        self.assertEqual((first["seen_before"], first["seen_after"], first["logged_after"]),
                         (before, after, logged))
        self.assertEqual(first["limit"], settings.BATCH_SIZE)
        # Окно одно на весь проход, даже если часы ушли вперёд.
        self.assertEqual(self.repo.calls[0]["seen_before"], self.repo.calls[1]["seen_before"])

    async def test_publish_error_rolls_back_and_unlocks(self):
        service = self.make(batches=[peers(2), peers(2, 5)], publish_error=RuntimeError("redis"))
        with self.assertLogs("app.reminders.service", "ERROR"):
            self.assertEqual(await service.run_once(), 0)
        self.assertEqual(self.log, ["lock", "begin", "claim", "publish", "rollback", "unlock"])

    async def test_run_survives_pass_error(self):
        service = self.make()

        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError("boom")

        service.run_once = broken

        async def stop(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) >= 3:
                raise asyncio.CancelledError

        service._sleep = stop
        with self.assertLogs("app.reminders.service", "ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await service.run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps[0], 60)
        self.assertEqual(self.sleeps[1], settings.CHECK_EVERY_MINUTES * 60)

    async def test_no_photo_file(self):
        service = self.make(batches=[peers(1)])
        await service.run_once()
        self.assertEqual(self.media.saved, [])
        self.assertEqual(self.events.calls[0][2:], (None, None))

    async def test_photo_uploaded_once_for_two_passes(self):
        self.photo.write_bytes(b"\xff\xd8\xff fake")
        service = self.make(batches=[peers(1), peers(1, 5)])
        await service.run_once()
        await service.run_once()
        self.assertEqual(len(self.media.saved), 1)
        self.assertEqual(self.media.saved[0][0], Peer("web", "broadcast", 0))
        self.assertEqual(self.media.saved[0][1], "photo")
        self.assertEqual(self.events.calls[0][2:], ("11111111-1111-1111-1111-111111111111",
                                                    "image/jpeg"))
        self.assertEqual(self.events.calls[1][2:], self.events.calls[0][2:])

    async def test_photo_reuploaded_when_file_changes(self):
        self.photo.write_bytes(b"one")
        service = self.make()
        await service._photo()
        self.photo.write_bytes(b"two")
        await service._photo()
        self.assertEqual(len(self.media.saved), 2)

    async def test_storage_errors_fall_back_to_text(self):
        self.photo.write_bytes(b"data")
        for error in (StorageDisabled(), MediaRejected(415, "не фото")):
            service = self.make(batches=[peers(1)], media=Media(error))
            with self.assertLogs("app.reminders.service", "WARNING"):
                sent = await service.run_once()
            self.assertEqual(sent, 1)
            self.assertEqual(self.events.calls[0][2:], (None, None))
            self.assertIsNone(service._photo_cache)


if __name__ == "__main__":
    unittest.main()
