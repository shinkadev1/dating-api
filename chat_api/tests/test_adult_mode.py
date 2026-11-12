"""Режим «Пошлый чат» (adult): маршруты без базы и подбор пар на настоящем Postgres.

Часть про базу: TEST_DATABASE_URL=postgresql://user:pass@host:port/db
Запуск из chat_api/: python -m pytest tests/test_adult_mode.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import create_pool, ensure_schema
from app.deps import get_chat
from app.peer import Peer
from app.routers import engine as engine_router
from app.routers import peers
from app.schemas import EngineDemandOut, StateOut
from app.services.chat import ADULT_NEEDS_PREMIUM, AdultNeedsPremiumError, ChatService

SERVICE = "service-token-0001"
ENGINE_TOKEN = "engine-token-000003"
AUTH = {"Authorization": f"Bearer {SERVICE}"}
ENGINE_AUTH = {"Authorization": f"Bearer {ENGINE_TOKEN}"}

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if sys.platform == "win32":
    # psycopg async не работает на ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FakeChat:
    def __init__(self, premium: bool = True) -> None:
        self.premium = premium
        self.search_modes: list[str] = []
        self.hunt_modes: list[str] = []

    async def search(self, peer: Peer, mode: str = "normal") -> StateOut:
        if mode == "adult" and not self.premium:
            raise AdultNeedsPremiumError
        self.search_modes.append(mode)
        return StateOut(phase="searching")

    async def engine_hunt(self, engine: Peer, mode: str = "normal") -> StateOut:
        self.hunt_modes.append(mode)
        return StateOut(phase="searching")

    async def engine_demand(self) -> EngineDemandOut:
        return EngineDemandOut(waiting=0, hunting=0)


def make_client(chat: FakeChat) -> TestClient:
    settings = Settings(
        database_url="postgresql://unused",
        chat_api_service_token=SERVICE,
        chat_api_app_token="app-token-00000002",
        chat_api_engine_token=ENGINE_TOKEN,
    )
    app = FastAPI()
    app.include_router(peers.router, prefix="/api/v1")
    app.include_router(engine_router.router, prefix="/api/v1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_chat] = lambda: chat
    return TestClient(app)


SEARCH = "/api/v1/peers/vk/club1/77/search"


class RouteTests(unittest.TestCase):
    def test_search_without_body_is_normal(self) -> None:
        chat = FakeChat()
        client = make_client(chat)
        self.assertEqual(client.post(SEARCH, headers=AUTH).status_code, 200)
        self.assertEqual(client.post(SEARCH, headers=AUTH, content=b"").status_code, 200)
        self.assertEqual(client.post(SEARCH, headers=AUTH, json={}).status_code, 200)
        self.assertEqual(chat.search_modes, ["normal", "normal", "normal"])

    def test_search_adult_passes_mode(self) -> None:
        chat = FakeChat()
        response = make_client(chat).post(SEARCH, headers=AUTH, json={"mode": "adult"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(chat.search_modes, ["adult"])

    def test_adult_without_premium_is_403_with_premium_word(self) -> None:
        response = make_client(FakeChat(premium=False)).post(SEARCH, headers=AUTH, json={"mode": "adult"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": ADULT_NEEDS_PREMIUM})
        self.assertIn("премиум", response.text.casefold())

    def test_invalid_mode_is_422(self) -> None:
        response = make_client(FakeChat()).post(SEARCH, headers=AUTH, json={"mode": "spicy"})
        self.assertEqual(response.status_code, 422)

    def test_hunt_mode_optional(self) -> None:
        chat = FakeChat()
        client = make_client(chat)
        self.assertEqual(client.post("/api/v1/engine/spec1/hunt", headers=ENGINE_AUTH).status_code, 200)
        self.assertEqual(
            client.post("/api/v1/engine/spec1/hunt", headers=ENGINE_AUTH, json={"mode": "adult"}).status_code, 200
        )
        self.assertEqual(chat.hunt_modes, ["normal", "adult"])


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Peer, object]] = []

    async def publish(self, peer: Peer, event: object) -> None:
        self.published.append((peer, event))

    async def request_engine(self) -> None:
        pass


A = Peer("tg", "bot", 11)
B = Peer("tg", "bot", 22)
C = Peer("vk", "club1", 33)
D = Peer("tg", "bot", 44)
SPEC = Peer("engine", "spec1", 1)
MAIN = Peer("engine", "main", 1)


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL не задан")
class AdultDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = create_pool(DATABASE_URL or "")
        await self.pool.open(wait=True, timeout=30)
        await ensure_schema(self.pool)
        async with self.pool.connection() as conn:
            await conn.execute("TRUNCATE messages, message_archives, dialogs, chat_sessions, bot_users CASCADE")
        self.events = FakeEvents()
        settings = Settings(database_url=DATABASE_URL or "", chat_api_service_token=SERVICE, chat_api_app_token="app-token-00000002")
        self.chat = ChatService(self.pool, self.events, settings)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def premium(self, *peers_: Peer, active: bool = True) -> None:
        interval = "NOW() + INTERVAL '30 days'" if active else "NOW() - INTERVAL '1 day'"
        async with self.pool.connection() as conn:
            for peer in peers_:
                await conn.execute(
                    f"INSERT INTO bot_users (platform, user_id, premium_until) VALUES (%s, %s, {interval}) "  # noqa: S608
                    "ON CONFLICT (platform, user_id) DO UPDATE SET premium_until = EXCLUDED.premium_until",
                    (peer.platform, peer.user_id),
                )

    async def session(self, peer: Peer) -> tuple[str, str]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT phase, mode FROM chat_sessions WHERE platform = %s AND bot_key = %s AND user_id = %s",
                (peer.platform, peer.bot_key, peer.user_id),
            )
            row = await cursor.fetchone()
        return row["phase"], row["mode"]

    async def test_migration_idempotent_and_default(self) -> None:
        await ensure_schema(self.pool)
        state = await self.chat.search(A)
        self.assertEqual(state.phase, "searching")
        self.assertEqual(await self.session(A), ("searching", "normal"))

    async def test_adult_without_premium_rejected_and_not_queued(self) -> None:
        with self.assertRaises(AdultNeedsPremiumError):
            await self.chat.search(A, "adult")
        await self.premium(B, active=False)
        with self.assertRaises(AdultNeedsPremiumError):
            await self.chat.search(B, "adult")
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) AS n FROM chat_sessions")
            self.assertEqual((await cursor.fetchone())["n"], 0)

    async def test_adult_with_premium_is_queued(self) -> None:
        await self.premium(A)
        state = await self.chat.search(A, "adult")
        self.assertEqual(state.phase, "searching")
        self.assertEqual(await self.session(A), ("searching", "adult"))

    async def test_adult_pairs_only_with_adult_humans(self) -> None:
        await self.premium(A, B, C, D)
        await self.chat.search(A, "normal")
        self.assertEqual((await self.chat.search(B, "adult")).phase, "searching")
        self.assertEqual((await self.chat.search(C, "adult")).phase, "chatting")
        self.assertEqual((await self.session(B))[0], "chatting")
        self.assertEqual(await self.session(A), ("searching", "normal"))
        self.assertEqual((await self.chat.search(D, "normal")).phase, "chatting")
        self.assertEqual((await self.session(A))[0], "chatting")

    async def test_normal_does_not_take_adult_waiter(self) -> None:
        await self.premium(A)
        await self.chat.search(A, "adult")
        self.assertEqual((await self.chat.search(B)).phase, "searching")
        self.assertEqual((await self.session(A))[0], "searching")

    async def test_engine_attach_takes_oldest_waiting_of_account_mode(self) -> None:
        await self.premium(A, B)
        await self.chat.search(A, "normal")  # дольше всех ждёт, но normal
        await self.chat.search(B, "adult")
        await self.chat.engine_hunt(SPEC, "adult")
        result = await self.chat.engine_attach(SPEC)
        self.assertTrue(result.attached)
        self.assertEqual((await self.session(B))[0], "chatting")
        self.assertEqual((await self.session(A))[0], "searching")
        # normal-аккаунт берёт normal-человека
        await self.chat.engine_hunt(MAIN, "normal")
        self.assertTrue((await self.chat.engine_attach(MAIN)).attached)
        self.assertEqual((await self.session(A))[0], "chatting")

    async def test_engine_attach_without_matching_mode_detaches(self) -> None:
        await self.chat.search(A, "normal")
        await self.chat.engine_hunt(SPEC, "adult")
        result = await self.chat.engine_attach(SPEC)
        self.assertFalse(result.attached)
        self.assertEqual(result.phase, "idle")
        self.assertEqual((await self.session(A))[0], "searching")

    async def test_demand_per_mode(self) -> None:
        await self.premium(B)
        await self.chat.search(A, "normal")
        await self.chat.search(B, "adult")
        await self.chat.engine_hunt(SPEC, "adult")
        demand = await self.chat.engine_demand()
        self.assertEqual((demand.waiting, demand.hunting), (2, 1))
        self.assertEqual((demand.modes["normal"].waiting, demand.modes["normal"].hunting), (1, 0))
        self.assertEqual((demand.modes["adult"].waiting, demand.modes["adult"].hunting), (1, 1))

    async def test_new_human_search_steals_engine_dialog_only_in_same_mode(self) -> None:
        await self.premium(A, B)
        await self.chat.search(A, "adult")
        await self.chat.engine_hunt(SPEC, "adult")
        await self.chat.engine_attach(SPEC)
        self.assertEqual((await self.chat.search(C, "normal")).phase, "searching")
        self.assertEqual((await self.session(A))[0], "chatting")
        self.assertEqual((await self.chat.search(B, "adult")).phase, "chatting")
        self.assertEqual((await self.session(SPEC))[0], "idle")

    async def test_report_researches_in_last_mode(self) -> None:
        await self.premium(A, B)
        await self.chat.search(A, "adult")
        await self.chat.search(B, "adult")
        await self.chat.report(A)
        self.assertEqual(await self.session(A), ("searching", "adult"))

    async def test_report_falls_back_to_normal_when_premium_expired(self) -> None:
        await self.premium(A, B)
        await self.chat.search(A, "adult")
        await self.chat.search(B, "adult")
        await self.premium(A, active=False)
        state = await self.chat.report(A)
        self.assertEqual(state.phase, "searching")
        self.assertEqual(await self.session(A), ("searching", "normal"))


if __name__ == "__main__":
    unittest.main()
