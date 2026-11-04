"""Старт S3-клиента не держит запуск API, когда бакет недоступен."""

import asyncio
import time
import unittest
from unittest.mock import patch

from app.services import storage


class _Client:
    def __init__(self, head):
        self._head = head

    async def head_bucket(self, **_):
        await self._head()


class _ClientCM:
    def __init__(self, client):
        self.client = client
        self.closed = False

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_):
        self.closed = True


class _Session:
    def __init__(self, cm):
        self._cm = cm

    def client(self, *_, **__):
        return self._cm


def _storage():
    return storage.ObjectStorage(endpoint="https://s3.example", region="ru1", bucket="b", access_key="a", secret_key="s")


class StorageStartupTests(unittest.IsolatedAsyncioTestCase):
    async def _open(self, head):
        cm = _ClientCM(_Client(head))
        s = _storage()
        with patch.object(storage.aioboto3, "Session", lambda: _Session(cm)), patch.object(storage, "STARTUP_CHECK_SECONDS", 0.2):
            started = time.monotonic()
            await s.open()
            return s, cm, time.monotonic() - started

    async def test_hanging_bucket_check_does_not_block_startup(self):
        async def hang():
            await asyncio.sleep(3600)

        s, cm, elapsed = await self._open(hang)
        self.assertLess(elapsed, 2)
        self.assertTrue(s.enabled)  # клиент остаётся, загрузки повторят попытку
        self.assertFalse(cm.closed)

    async def test_failed_bucket_check_keeps_client(self):
        async def fail():
            raise ConnectionError("down")

        s, cm, _ = await self._open(fail)
        self.assertTrue(s.enabled)
        self.assertFalse(cm.closed)

    async def test_healthy_bucket(self):
        async def ok():
            return None

        s, _, _ = await self._open(ok)
        self.assertTrue(s.enabled)

    async def test_access_denied_closes_client(self):
        class Denied(Exception):
            response = {"Error": {"Code": "403"}}

        async def deny():
            raise Denied()

        s, cm, _ = await self._open(deny)
        self.assertFalse(s.enabled)
        self.assertTrue(cm.closed)

    async def test_client_creation_failure_disables_storage(self):
        class Broken(_ClientCM):
            async def __aenter__(self):
                raise ValueError("bad endpoint")

        cm = Broken(None)
        s = _storage()
        with patch.object(storage.aioboto3, "Session", lambda: _Session(cm)):
            await s.open()
        self.assertFalse(s.enabled)
        self.assertTrue(cm.closed)

    def test_client_config_has_short_timeouts(self):
        config = storage._s3_config()
        self.assertEqual(config.connect_timeout, 5)
        self.assertEqual(config.read_timeout, 30)


if __name__ == "__main__":
    unittest.main()
