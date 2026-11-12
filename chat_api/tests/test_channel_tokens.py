"""Шифрование ключей сообществ ВК и проверка ключа через ВК (подмена httpx)."""

import asyncio
import base64
import sys
import unittest
from unittest import mock

import httpx

from app import channel_tokens, vk_groups
from app.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SECRET = "k" * 40
TOKEN = "vk1.a.abcdefghijklmnopqrstuvwxyz0123456789Qx7f"


class EncryptTest(unittest.TestCase):
    def test_roundtrip_and_format(self):
        sealed = channel_tokens.encrypt(SECRET, 123, TOKEN)
        self.assertTrue(sealed.startswith("v1:"))
        self.assertNotIn(TOKEN, sealed)
        self.assertEqual(channel_tokens.decrypt(SECRET, 123, sealed), TOKEN)

    def test_nonce_is_random(self):
        self.assertNotEqual(channel_tokens.encrypt(SECRET, 1, TOKEN), channel_tokens.encrypt(SECRET, 1, TOKEN))

    def test_other_group_other_key_or_garbage_fails(self):
        sealed = channel_tokens.encrypt(SECRET, 123, TOKEN)
        broken = "v1:" + base64.b64encode(b"x" * 40).decode()
        for secret, group, value in (
            (SECRET, 124, sealed),
            ("z" * 40, 123, sealed),
            (SECRET, 123, broken),
            (SECRET, 123, "v1:"),
            (SECRET, 123, "v1:!!!"),
            (SECRET, 123, TOKEN),
        ):
            with self.subTest(group=group, value=value[:6]), self.assertRaises(channel_tokens.TokenDecryptError):
                channel_tokens.decrypt(secret, group, value)

    def test_hint(self):
        self.assertEqual(channel_tokens.hint(TOKEN), "vk1.a.…Qx7f")
        self.assertEqual(channel_tokens.hint("short"), "…")

    def test_settings_key(self):
        base = dict(database_url="x", chat_api_service_token="s" * 20, chat_api_app_token="a" * 20)
        self.assertIsNone(Settings(_env_file=None, **base, channel_token_key="  ").channel_token_key)
        self.assertEqual(Settings(_env_file=None, **base, channel_token_key=SECRET).channel_token_key, SECRET)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, **base, channel_token_key="short")


def _client(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    return mock.patch.object(vk_groups.httpx, "AsyncClient", factory)


class CheckTokenTest(unittest.IsolatedAsyncioTestCase):
    async def test_ok(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            calls.append((method, str(request.url)))
            if method == "groups.getById":
                return httpx.Response(200, json={"response": {"groups": [{"id": 77, "name": "Клуб", "photo_200": "http://p"}]}})
            return httpx.Response(200, json={"response": {"is_enabled": True}})

        with _client(handler):
            info = await vk_groups.check_token(TOKEN)
        self.assertEqual((info.group_id, info.title, info.photo_url, info.long_poll), (77, "Клуб", "http://p", True))
        self.assertEqual([m for m, _ in calls], ["groups.getById", "groups.getLongPollSettings"])
        # ключ уходит в теле запроса, а не в адресе
        self.assertTrue(all(TOKEN not in url for _, url in calls))

    async def test_rejected(self):
        def handler(request):
            return httpx.Response(200, json={"error": {"error_code": 5, "error_msg": "User authorization failed"}})

        with _client(handler), self.assertRaises(vk_groups.VkRejected) as ctx:
            await vk_groups.check_token(TOKEN)
        self.assertEqual((ctx.exception.code, ctx.exception.msg), (5, "User authorization failed"))

    async def test_malformed_responses_are_unavailable(self):
        by_id = {"response": []}, {"response": 5}, {"response": {"groups": []}}, {"response": [{"name": "x"}]}
        for payload in by_id:
            with self.subTest(payload=str(payload)), _client(lambda r, p=payload: httpx.Response(200, json=p)):
                with self.assertRaises(vk_groups.VkUnavailable):
                    await vk_groups.check_token(TOKEN)

    async def test_non_numeric_error_code(self):
        def handler(request):
            return httpx.Response(200, json={"error": {"error_code": "abc", "error_msg": "bad"}})

        with _client(handler), self.assertRaises(vk_groups.VkRejected) as ctx:
            await vk_groups.check_token(TOKEN)
        self.assertEqual(ctx.exception.code, 0)

    async def test_unavailable(self):
        def boom(request):
            raise httpx.ConnectError("down")

        def junk(request):
            return httpx.Response(200, text="<html>")

        for handler in (boom, junk):
            with self.subTest(handler=handler.__name__), _client(handler), self.assertRaises(vk_groups.VkUnavailable):
                await vk_groups.check_token(TOKEN)
