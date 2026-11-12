"""Сообщества ВК из CRM на настоящем Postgres: TEST_DATABASE_URL или pgserver. ВК подменён."""

import asyncio
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import channel_tokens, vk_groups
from app.broadcasts.service import BroadcastService
from app.config import Settings, get_settings
from app.db import ensure_schema
from app.deps import current_client
from app.repositories import sessions
from app.routers import channels as channels_router

sys.path.insert(0, os.path.dirname(__file__))
from test_channel_tokens import seal  # noqa: E402
from test_reminders_db import _database_url  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
KEY = "k" * 40
TOKEN_A = "vk1.a.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1234"
TOKEN_B = "vk1.a.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB5678"
BASE = dict(database_url="x", chat_api_service_token="s" * 20, chat_api_app_token="a" * 20)


class FakeBus:
    def __init__(self):
        self.sent = []

    async def publish_notices(self, recipients, text, *, media_id=None, content_type=None):
        self.sent.extend((p.platform, p.bot_key, p.user_id) for p in recipients)


async def _no_sleep(_):
    return None


class ChannelsDbTest(unittest.IsolatedAsyncioTestCase):
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
        await ensure_schema(self.pool)  # 009 и 010 идемпотентны
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE broadcast_log, broadcasts, reminder_log, user_channels, chat_sessions, bot_users, "
                "dialogs, channels RESTART IDENTITY CASCADE"
            )
        # «ВК»: ключ -> сообщество
        self.vk = {TOKEN_A: (10, "Клуб А"), TOKEN_B: (20, "Клуб Б")}
        self.vk_error = None
        self.checks = []

        async def fake_check(token):
            self.checks.append(token)
            if self.vk_error:
                raise self.vk_error
            if token not in self.vk:
                raise vk_groups.VkRejected(5, "User authorization failed")
            gid, name = self.vk[token]
            return vk_groups.GroupInfo(gid, name, f"http://photo/{gid}", True)

        patcher = mock.patch.object(vk_groups, "check_token", fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(channels_router.router, prefix="/api/v1")
        self.client_kind = "crm"
        app.dependency_overrides[current_client] = lambda: self.client_kind
        app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **BASE)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)

    async def sql(self, query, args=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, args)
            return await cur.fetchall() if cur.description else None

    async def call(self, method, path, kind="crm", **kw):
        self.client_kind = kind
        return await self.api.request(method, "/api/v1" + path, **kw)

    async def connect(self, token=TOKEN_A):
        r = await self.call("POST", "/channels/vk", json={"token": token})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # ---------- миграция ----------

    async def test_migration_defaults_for_old_rows(self):
        await self.sql("INSERT INTO channels (platform, external_id, title) VALUES ('vk', '5', 'Старое')")
        await ensure_schema(self.pool)
        row = (await self.sql("SELECT enabled, status, token_encrypted, token_hint FROM channels"))[0]
        self.assertEqual((row["enabled"], row["status"], row["token_encrypted"], row["token_hint"]), (True, "unknown", None, ""))

    # ---------- роли ----------

    async def test_roles(self):
        body = {"token": TOKEN_A}
        crm_only = [
            ("POST", "/channels/vk/check", body),
            ("POST", "/channels/vk", body),
            ("PUT", "/channels/vk/10/token", body),
            ("PATCH", "/channels/vk/10", {"enabled": False}),
        ]
        bot_only = [
            ("GET", "/channels/vk/tokens", None),
            ("POST", "/channels/vk/import", {"group_id": 1, "token": TOKEN_A}),
            ("POST", "/channels/vk/status", {"items": []}),
        ]
        for kind in ("bot", "tg_bot", "app", "engine"):
            for method, path, js in crm_only:
                with self.subTest(kind=kind, path=path):
                    r = await self.call(method, path, kind=kind, json=js)
                    self.assertEqual(r.status_code, 403, r.text)
        for kind in ("crm", "tg_bot", "app", "engine"):
            for method, path, js in bot_only:
                with self.subTest(kind=kind, path=path):
                    r = await self.call(method, path, kind=kind, json=js)
                    self.assertEqual(r.status_code, 403, r.text)
        for kind in ("tg_bot", "app", "engine"):
            self.assertEqual((await self.call("GET", "/channels/vk", kind=kind)).status_code, 403)
        for kind in ("crm", "bot"):
            self.assertEqual((await self.call("GET", "/channels/vk", kind=kind)).status_code, 200)

    async def test_no_token_is_401(self):
        app = FastAPI()
        app.state.pool = self.pool
        app.include_router(channels_router.router, prefix="/api/v1")
        app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **BASE)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            for path in ("/channels/vk", "/channels/vk/tokens"):
                self.assertEqual((await api.get("/api/v1" + path)).status_code, 401)
            r = await api.get("/api/v1/channels/vk", headers={"Authorization": "Bearer wrong-wrong-wrong-1"})
            self.assertEqual(r.status_code, 401)

    # ---------- check / connect ----------

    async def test_check_does_not_write(self):
        r = await self.call("POST", "/channels/vk/check", json={"token": TOKEN_A})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json(),
            {"group_id": 10, "title": "Клуб А", "photo_url": "http://photo/10", "long_poll": True, "connected": False, "enabled": None},
        )
        self.assertEqual(await self.sql("SELECT 1 FROM channels"), [])
        await self.connect()
        r = await self.call("POST", "/channels/vk/check", json={"token": TOKEN_A})
        self.assertEqual((r.json()["connected"], r.json()["enabled"]), (True, True))

    async def test_long_token_is_not_echoed(self):
        secret = "S3CRET" * 100
        for path, method, extra in (
            ("/channels/vk/check", "POST", {}),
            ("/channels/vk", "POST", {}),
            ("/channels/vk/10/token", "PUT", {}),
        ):
            with self.subTest(path=path):
                r = await self.call(method, path, json={"token": secret})
                self.assertEqual(r.status_code, 422)
                self.assertNotIn("S3CRET", r.text)
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 1, "token": secret})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("S3CRET", r.text)
        self.assertEqual(self.checks, [])

    async def test_check_errors(self):
        for body in ({"token": ""}, {"token": "   "}, {"token": "x" * 513}, {}):
            with self.subTest(body=str(body)[:20]):
                r = await self.call("POST", "/channels/vk/check", json=body)
                self.assertEqual(r.status_code, 422)
        r = await self.call("POST", "/channels/vk/check", json={"token": "bad"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "Ключ не подошёл: User authorization failed (код 5)")
        self.vk_error = vk_groups.VkUnavailable("ConnectError")
        r = await self.call("POST", "/channels/vk/check", json={"token": TOKEN_A})
        self.assertEqual(r.status_code, 502)

    async def test_connect_stores_plain_and_hides_token_from_crm(self):
        out = await self.connect()
        self.assertEqual(
            out,
            {
                "group_id": 10, "title": "Клуб А", "photo_url": "http://photo/10",
                "token_hint": "vk1.a.…1234", "has_token": True, "enabled": True,
                "status": "unknown", "error": "", "last_seen_at": None,
            },
        )
        row = (await self.sql("SELECT * FROM channels"))[0]
        self.assertEqual((row["token"], row["token_encrypted"]), (TOKEN_A, None))
        self.assertEqual((row["platform"], row["external_id"]), ("vk", "10"))
        listing = await self.call("GET", "/channels/vk")
        for kind in ("crm", "bot"):
            listing = await self.call("GET", "/channels/vk", kind=kind)
            self.assertNotIn(TOKEN_A, listing.text)
            self.assertNotIn(TOKEN_A[6:30], listing.text)
            self.assertEqual(listing.json()[0]["group_id"], 10)

    async def test_works_without_channel_token_key(self):
        # Settings собраны без CHANNEL_TOKEN_KEY: connect, import и tokens работают.
        await self.connect()
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 30, "token": TOKEN_B})
        self.assertEqual((r.status_code, r.json()), (200, {"stored": True}))
        r = await self.call("GET", "/channels/vk/tokens", kind="bot")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({c["group_id"]: c["token"] for c in r.json()["communities"]}, {10: TOKEN_A, 30: TOKEN_B})
        # CRM не получает полный ключ ни в одном ответе
        for resp in (await self.call("GET", "/channels/vk"), await self.call("POST", "/channels/vk/check", json={"token": TOKEN_A})):
            self.assertNotIn(TOKEN_A, resp.text)
        self.assertEqual((await self.call("GET", "/channels/vk/tokens", kind="crm")).status_code, 403)

    async def test_connect_reenables_and_keeps_old_channel_data(self):
        await self.sql("INSERT INTO channels (platform, external_id, title, enabled) VALUES ('vk', '10', '', FALSE)")
        out = await self.connect()
        self.assertEqual((out["enabled"], out["title"]), (True, "Клуб А"))
        self.assertEqual(len(await self.sql("SELECT 1 FROM channels")), 1)

    # ---------- замена ключа ----------

    async def test_replace_token(self):
        await self.connect()
        self.vk["new-key-for-a"] = (10, "Клуб А+")
        r = await self.call("PUT", "/channels/vk/10/token", json={"token": "new-key-for-a"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["title"], r.json()["token_hint"]), ("unknown", "Клуб А+", "…"))
        r = await self.call("GET", "/channels/vk/tokens", kind="bot")
        self.assertEqual(r.json()["communities"], [{"group_id": 10, "title": "Клуб А+", "token": "new-key-for-a"}])

    async def test_replace_token_conflicts(self):
        await self.connect()
        before = (await self.sql("SELECT token FROM channels"))[0]["token"]
        r = await self.call("PUT", "/channels/vk/10/token", json={"token": TOKEN_B})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "Ключ от другого сообщества"))
        self.assertEqual((await self.sql("SELECT token FROM channels"))[0]["token"], before)
        r = await self.call("PUT", "/channels/vk/20/token", json={"token": TOKEN_B})
        self.assertEqual(r.status_code, 404)
        r = await self.call("PUT", "/channels/vk/10/token", json={"token": "bad"})
        self.assertEqual(r.status_code, 422)

    # ---------- переключатель, ключи для бота ----------

    async def test_toggle_and_tokens_only_enabled(self):
        await self.connect(TOKEN_A)
        await self.connect(TOKEN_B)
        await self.sql("INSERT INTO channels (platform, external_id, title) VALUES ('vk', '30', 'Без ключа')")
        r = await self.call("GET", "/channels/vk/tokens", kind="bot")
        self.assertEqual([c["group_id"] for c in r.json()["communities"]], [10, 20])
        self.assertEqual(r.json()["communities"][0]["token"], TOKEN_A)
        self.assertEqual(r.json()["disabled"], [])

        r = await self.call("PATCH", "/channels/vk/10", json={"enabled": False})
        self.assertEqual((r.status_code, r.json()["enabled"], r.json()["status"]), (200, False, "disabled"))
        r = await self.call("GET", "/channels/vk/tokens", kind="bot")
        self.assertEqual([c["group_id"] for c in r.json()["communities"]], [20])
        self.assertEqual(r.json()["disabled"], [10])
        # ключ остался
        self.assertTrue(next(c for c in (await self.call("GET", "/channels/vk")).json() if c["group_id"] == 10)["has_token"])

        r = await self.call("PATCH", "/channels/vk/10", json={"enabled": True})
        self.assertEqual(r.json()["status"], "unknown")
        self.assertEqual((await self.call("PATCH", "/channels/vk/999", json={"enabled": True})).status_code, 404)
        self.assertEqual((await self.call("PATCH", "/channels/vk/10", json={})).status_code, 422)

    # ---------- перенос старых зашифрованных ключей ----------

    async def old_row(self, gid, value, title="Старое"):
        await self.sql(
            "INSERT INTO channels (platform, external_id, title, token_encrypted, token_hint) VALUES ('vk', %s, %s, %s, 'h')",
            (str(gid), title, value),
        )

    async def test_migration_moves_old_tokens(self):
        await self.old_row(10, seal(KEY, 10, TOKEN_A))
        await self.old_row(20, seal(KEY, 20, TOKEN_B))
        await self.sql("INSERT INTO channels (platform, external_id, title) VALUES ('vk', '30', 'Без ключа')")
        with self.assertLogs("app.channel_tokens", level=logging.INFO) as logs:
            self.assertEqual(await channel_tokens.migrate_encrypted(self.pool, KEY), 2)
        self.assertIn("Перенесено токенов сообществ ВК: 2", "\n".join(logs.output))
        self.assertNotIn(TOKEN_A, "\n".join(logs.output))
        rows = {r["external_id"]: r for r in await self.sql("SELECT * FROM channels")}
        self.assertEqual((rows["10"]["token"], rows["10"]["token_encrypted"]), (TOKEN_A, None))
        self.assertEqual((rows["20"]["token"], rows["20"]["token_encrypted"]), (TOKEN_B, None))
        self.assertEqual((rows["30"]["token"], rows["30"]["token_encrypted"]), (None, None))
        r = await self.call("GET", "/channels/vk/tokens", kind="bot")
        self.assertEqual([c["token"] for c in r.json()["communities"]], [TOKEN_A, TOKEN_B])
        # повтор ничего не делает
        self.assertEqual(await channel_tokens.migrate_encrypted(self.pool, KEY), 0)
        self.assertEqual({r["external_id"]: r["token"] for r in await self.sql("SELECT * FROM channels")}["10"], TOKEN_A)

    async def test_migration_without_key_leaves_rows_and_does_not_fail(self):
        sealed = seal(KEY, 10, TOKEN_A)
        await self.old_row(10, sealed)
        with self.assertLogs("app.channel_tokens", level=logging.WARNING) as logs:
            self.assertEqual(await channel_tokens.migrate_encrypted(self.pool, None), 0)
        self.assertIn("10", "\n".join(logs.output))
        row = (await self.sql("SELECT token, token_encrypted FROM channels"))[0]
        self.assertEqual((row["token"], row["token_encrypted"]), (None, sealed))
        # строка без ключа: has_token = false, tokens её не отдаёт, import заполняет
        self.assertFalse((await self.call("GET", "/channels/vk")).json()[0]["has_token"])
        self.assertEqual((await self.call("GET", "/channels/vk/tokens", kind="bot")).json()["communities"], [])
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 10, "token": TOKEN_A})
        self.assertEqual(r.json(), {"stored": True})
        row = (await self.sql("SELECT token, token_encrypted FROM channels"))[0]
        self.assertEqual((row["token"], row["token_encrypted"]), (TOKEN_A, None))

    async def test_migration_skips_broken_rows_without_logging_token(self):
        good = seal(KEY, 20, TOKEN_B)
        await self.old_row(10, "v1:AAAA")
        await self.old_row(20, good)
        await self.old_row(30, seal("z" * 40, 30, TOKEN_A))  # другой ключ
        with self.assertLogs("app.channel_tokens", level=logging.INFO) as logs:
            self.assertEqual(await channel_tokens.migrate_encrypted(self.pool, KEY), 1)
        text = "\n".join(logs.output)
        self.assertIn("10", text)
        for secret in (TOKEN_A, TOKEN_B, "AAAA", good):
            self.assertNotIn(secret, text)
        rows = {r["external_id"]: r for r in await self.sql("SELECT * FROM channels")}
        self.assertEqual((rows["10"]["token"], rows["10"]["token_encrypted"]), (None, "v1:AAAA"))
        self.assertEqual(rows["20"]["token"], TOKEN_B)
        self.assertIsNone(rows["30"]["token"])
        # сломанные строки не мешают следующему запуску
        self.assertEqual(await channel_tokens.migrate_encrypted(self.pool, KEY), 0)

    # ---------- import ----------

    async def test_import_does_not_overwrite_or_enable(self):
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 10, "title": "Клуб А", "token": TOKEN_A})
        self.assertEqual((r.status_code, r.json()), (200, {"stored": True}))
        enc = (await self.sql("SELECT token FROM channels"))[0]["token"]
        self.assertEqual(enc, TOKEN_A)
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 10, "title": "x", "token": TOKEN_B})
        self.assertEqual(r.json(), {"stored": False})
        row = (await self.sql("SELECT * FROM channels"))[0]
        self.assertEqual((row["token"], row["title"]), (enc, "Клуб А"))

        await self.call("PATCH", "/channels/vk/10", json={"enabled": False})
        await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 10, "token": TOKEN_B})
        self.assertFalse((await self.sql("SELECT enabled FROM channels"))[0]["enabled"])

    async def test_import_fills_missing_token_without_touching_enabled(self):
        await self.sql("INSERT INTO channels (platform, external_id, title, enabled) VALUES ('vk', '10', '', FALSE)")
        r = await self.call("POST", "/channels/vk/import", kind="bot", json={"group_id": 10, "title": "Клуб А", "token": TOKEN_A})
        self.assertEqual(r.json(), {"stored": True})
        row = (await self.sql("SELECT * FROM channels"))[0]
        self.assertEqual((row["enabled"], row["title"], row["status"]), (False, "Клуб А", "unknown"))
        self.assertEqual(row["token_hint"], "vk1.a.…1234")

    # ---------- status ----------

    async def test_status_sets_last_seen_and_ignores_unknown(self):
        await self.connect()
        await self.sql("INSERT INTO channels (platform, external_id) VALUES ('tg', '10')")
        r = await self.call(
            "POST", "/channels/vk/status", kind="bot",
            json={"items": [{"group_id": 10, "status": "error", "error": "e" * 900}, {"group_id": 99, "status": "ok"}]},
        )
        self.assertEqual(r.status_code, 204)
        out = (await self.call("GET", "/channels/vk")).json()
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["status"], len(out[0]["error"])), ("error", 500))
        self.assertIsNotNone(out[0]["last_seen_at"])
        self.assertEqual((await self.sql("SELECT status, last_seen_at FROM channels WHERE platform = 'tg'"))[0]["last_seen_at"], None)
        await self.call("POST", "/channels/vk/status", kind="bot", json={"items": [{"group_id": 10, "status": "ok"}]})
        out = (await self.call("GET", "/channels/vk")).json()
        self.assertEqual((out[0]["status"], out[0]["error"]), ("ok", ""))
        # отключённое показывается как disabled, даже если бот прислал ok
        await self.call("PATCH", "/channels/vk/10", json={"enabled": False})
        self.assertEqual((await self.call("GET", "/channels/vk")).json()[0]["status"], "disabled")

    async def test_status_limit_and_values(self):
        too_many = {"items": [{"group_id": 1, "status": "ok"}] * 201}
        self.assertEqual((await self.call("POST", "/channels/vk/status", kind="bot", json=too_many)).status_code, 422)
        bad = {"items": [{"group_id": 1, "status": "disabled"}]}
        self.assertEqual((await self.call("POST", "/channels/vk/status", kind="bot", json=bad)).status_code, 422)

    # ---------- отключённые не получают сообщений ----------

    async def test_disabled_excluded_from_notices_and_broadcasts(self):
        await self.connect(TOKEN_A)
        await self.connect(TOKEN_B)
        for gid in (10, 20):
            ch = (await self.sql("SELECT id FROM channels WHERE external_id = %s", (str(gid),)))[0]["id"]
            uid = 1000 + gid
            await self.sql("INSERT INTO bot_users (platform, user_id) VALUES ('vk', %s)", (uid,))
            await self.sql(
                "INSERT INTO user_channels (user_platform, user_id, channel_id, started_at, last_seen_at) "
                "VALUES ('vk', %s, %s, %s, %s)",
                (uid, ch, NOW - timedelta(days=10), NOW - timedelta(days=1)),
            )
        async with self.pool.connection() as conn:
            self.assertEqual(sorted(p.bot_key for p in await sessions.channel_recipients(conn)), ["10", "20"])
        await self.call("PATCH", "/channels/vk/10", json={"enabled": False})
        async with self.pool.connection() as conn:
            self.assertEqual([p.bot_key for p in await sessions.channel_recipients(conn)], ["20"])

        bus = FakeBus()
        service = BroadcastService(self.pool, bus, clock=lambda: NOW + timedelta(hours=2), sleep=_no_sleep)
        await self.sql(
            "INSERT INTO broadcasts (title, text, platforms, kind, send_at, status, active_since) "
            "VALUES ('t', 'hi', %s, 'at', %s, 'scheduled', %s)",
            (["vk"], NOW + timedelta(hours=1), NOW - timedelta(days=30)),
        )
        await self.sql(
            "INSERT INTO broadcasts (title, text, platforms, kind, event, delay_minutes, repeat, status, active_since) "
            "VALUES ('e', 'yo', %s, 'event', 'first_seen', 0, FALSE, 'active', %s)",
            (["vk"], NOW - timedelta(days=30)),
        )
        await service.run_once()
        self.assertEqual({(p, k) for p, k, _ in bus.sent}, {("vk", "20")})
        self.assertEqual(len(bus.sent), 2)  # по рассылке на человека из включённого сообщества

        from app.repositories import broadcasts as brepo

        async with self.pool.connection() as conn:
            self.assertEqual(await brepo.audience(conn, ["vk"]), 1)
