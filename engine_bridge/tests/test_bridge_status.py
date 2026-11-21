"""Мост сообщает Chat API ошибки аккаунта для журнала инцидентов поиска."""

from __future__ import annotations

import unittest

import httpx

from app.bridge import Bridge
from app.chat_api import ChatApiClient, ChatApiError
from test_bridge_limited import FakeChatApi, FakeEngine, make_bridge


class BrokenStatusChatApi(FakeChatApi):
    async def status(self, account, mode=None, *, error=None, clear=False):  # type: ignore[no-untyped-def]
        raise ChatApiError("POST status: HTTP 500")


class BridgeStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_409_reports_error_and_mode(self) -> None:
        chat_api, engine = FakeChatApi(waiting=1), FakeEngine(conflict={"main"})
        bridge = make_bridge(chat_api, engine, {"main": "idle"}, {"main": "any"})
        await bridge._reconcile()
        self.assertEqual(len(chat_api.statuses), 1)
        account, mode, error, clear = chat_api.statuses[0]
        self.assertEqual((account, mode, clear), ("main", "any", False))
        self.assertTrue(error.startswith("409 cooldown"))
        self.assertTrue(bridge._links["main"].error_reported)

    async def test_successful_start_clears_reported_error(self) -> None:
        chat_api, engine = FakeChatApi(waiting=1), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "idle"})
        bridge._links["main"].error_reported = True
        await bridge._reconcile()
        self.assertEqual(chat_api.statuses, [("main", "normal", None, True)])
        self.assertFalse(bridge._links["main"].error_reported)

    async def test_second_successful_start_sends_nothing(self) -> None:
        chat_api, engine = FakeChatApi(waiting=1), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "idle"})
        bridge._links["main"].error_reported = False
        await bridge._reconcile()
        self.assertEqual(chat_api.statuses, [])

    async def test_limited_reports_error_and_return_to_idle_clears(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "searching"})
        link = bridge._links["main"]
        await bridge._on_engine_event(link, {"type": "state", "state": "limited"})
        self.assertEqual(chat_api.statuses, [("main", "normal", "limited", False)])
        await bridge._on_engine_event(link, {"type": "state", "state": "idle"})
        self.assertEqual(chat_api.statuses[-1], ("main", "normal", None, True))

    async def test_clear_after_restart_in_limited_is_forced(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "limited"})  # error_reported=False, как после рестарта
        await bridge._on_engine_event(bridge._links["main"], {"type": "state", "state": "idle"})
        self.assertEqual(chat_api.statuses, [("main", "normal", None, True)])

    async def test_snapshot_idle_clears_stale_limited(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": None})
        link = bridge._links["main"]
        link.error_reported = False  # как после рестарта моста с принудительной очисткой
        await bridge._on_engine_event(link, {"type": "snapshot", "state": "idle"})
        self.assertIn(("main", "normal", None, True), chat_api.statuses)

    async def test_snapshot_limited_does_not_clear(self) -> None:
        chat_api, engine = FakeChatApi(phase="idle"), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": None})
        await bridge._on_engine_event(bridge._links["main"], {"type": "snapshot", "state": "limited"})
        self.assertNotIn(("main", "normal", None, True), chat_api.statuses)

    async def test_new_link_clears_on_first_successful_start(self) -> None:
        from app.bridge import AccountLink

        self.assertTrue(AccountLink("x").error_reported)

    async def test_heartbeat_sends_mode_only_on_change_or_every_tenth_cycle(self) -> None:
        import asyncio

        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "idle"})

        async def visit(account: str) -> None:
            pass

        chat_api.visit = visit  # type: ignore[attr-defined]
        calls = 0

        async def fake_sleep(delay: float) -> None:
            nonlocal calls
            calls += 1
            if calls >= 11:
                raise asyncio.CancelledError

        import app.bridge as bridge_module

        original = bridge_module.asyncio.sleep
        bridge_module.asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            with self.assertRaises(asyncio.CancelledError):
                await bridge._heartbeat_loop()
        finally:
            bridge_module.asyncio.sleep = original  # type: ignore[assignment]
        # цикл 1 (первая отправка) и цикл 10; на остальных тихо
        self.assertEqual(len(chat_api.statuses), 2)

    async def test_status_failure_does_not_break_bridge(self) -> None:
        chat_api, engine = BrokenStatusChatApi(waiting=1), FakeEngine(conflict={"main", "second"})
        bridge = make_bridge(chat_api, engine, {"main": "idle", "second": "idle"})
        with self.assertLogs("app.bridge", level="WARNING"):
            await bridge._reconcile()
        # обработка 409 дошла до конца: пауза выставлена, поиск человека снят
        self.assertGreater(bridge._links["main"].cooldown_until, 0.0)
        self.assertIn(("stop", "main"), chat_api.calls)
        self.assertTrue(bridge._wake.is_set())


class ClientStatusTest(unittest.IsolatedAsyncioTestCase):
    def client(self, handler) -> ChatApiClient:  # type: ignore[no-untyped-def]
        client = ChatApiClient("http://chat-api", "token")
        client._http = httpx.AsyncClient(base_url="http://chat-api/api/v1", transport=httpx.MockTransport(handler))
        return client

    async def test_body_shapes(self) -> None:
        bodies: list[tuple[str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append((request.url.path, request.content))
            return httpx.Response(204)

        client = self.client(handler)
        await client.status("acc", "any")
        await client.status("acc", "any", error="409")
        await client.status("acc", "any", clear=True)
        await client.aclose()
        self.assertEqual(
            bodies,
            [
                ("/api/v1/engine/acc/status", b'{"mode":"any"}'),
                ("/api/v1/engine/acc/status", b'{"mode":"any","error":"409"}'),
                ("/api/v1/engine/acc/status", b'{"mode":"any","error":null}'),
            ],
        )

    async def test_404_from_old_chat_api_is_not_an_error(self) -> None:
        client = self.client(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
        self.assertFalse(await client.status("acc", "normal", error="409"))
        await client.aclose()

    async def test_old_chat_api_404_does_not_break_bridge(self) -> None:
        chat_api = FakeChatApi(waiting=1)
        real = self.client(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
        chat_api.status = real.status  # type: ignore[method-assign]
        engine = FakeEngine(conflict={"main"})
        bridge = make_bridge(chat_api, engine, {"main": "idle"})
        await bridge._reconcile()
        self.assertGreater(bridge._links["main"].cooldown_until, 0.0)
        await real.aclose()

    async def test_500_is_an_error(self) -> None:
        client = self.client(lambda request: httpx.Response(500, text="boom"))
        with self.assertRaises(ChatApiError):
            await client.status("acc", "normal", error="409")
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
