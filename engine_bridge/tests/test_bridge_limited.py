"""Мост и лимит аккаунта chat-engine: состояние limited, cooldown после 409."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx

from app.bridge import AccountLink, Bridge
from app.engine import EngineAccount, EngineClient, EngineConflictError


class FakeChatApi:
    def __init__(self, phase: str = "idle", waiting: int = 1, hunting: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._phase = phase
        self._demand = SimpleNamespace(waiting=waiting, hunting=hunting)

    async def phase(self, account: str) -> str:
        return self._phase

    async def demand(self):
        return self._demand

    async def hunt(self, account: str) -> None:
        self.calls.append(("hunt", account))

    async def stop(self, account: str) -> None:
        self.calls.append(("stop", account))

    async def attach(self, account: str) -> bool:
        self.calls.append(("attach", account))
        return True


class FakeEngine:
    def __init__(self, conflict: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._conflict = conflict or set()

    async def start(self, account: str) -> None:
        self.calls.append(("start", account))
        if account in self._conflict:
            raise EngineConflictError("limited")

    async def stop(self, account: str) -> None:
        self.calls.append(("stop", account))


def make_bridge(chat_api: FakeChatApi, engine: FakeEngine, states: dict[str, str | None]) -> Bridge:
    bridge = Bridge(chat_api, engine, reconcile_interval=1, accounts_refresh=1, heartbeat=1)  # type: ignore[arg-type]
    for account, state in states.items():
        bridge._links[account] = AccountLink(account, state=state)
    return bridge


class BridgeLimitedTest(unittest.IsolatedAsyncioTestCase):
    async def test_limited_state_event_stops_human_side_only(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "searching"})
        link = bridge._links["main"]
        await bridge._on_engine_event(link, {"type": "state", "state": "limited"})
        self.assertEqual(link.state, "limited")
        self.assertEqual(chat_api.calls, [("stop", "main")])
        self.assertEqual(engine.calls, [])
        self.assertTrue(bridge._wake.is_set())

    async def test_message_event_with_limited_state(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "searching"})
        link = bridge._links["main"]
        await bridge._on_engine_event(link, {"type": "message", "state": "limited", "message": {"text": "x"}})
        self.assertEqual(link.state, "limited")
        self.assertEqual(chat_api.calls, [("stop", "main")])
        self.assertEqual(engine.calls, [])

    async def test_reconcile_skips_limited(self) -> None:
        chat_api, engine = FakeChatApi(waiting=1), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "limited", "second": "idle"})
        await bridge._reconcile()
        self.assertEqual(chat_api.calls, [("hunt", "second")])
        self.assertEqual(engine.calls, [("start", "second")])
        self.assertEqual(bridge._links["second"].state, "searching")

    async def test_conflict_sets_cooldown_and_next_account_is_used(self) -> None:
        chat_api, engine = FakeChatApi(waiting=1), FakeEngine(conflict={"second"})
        bridge = make_bridge(chat_api, engine, {"main": "limited", "second": "idle", "third": "idle"})
        # second идёт первым в порядке словаря
        await bridge._reconcile()
        self.assertEqual(chat_api.calls, [("hunt", "second"), ("stop", "second")])
        self.assertGreater(bridge._links["second"].cooldown_until, 0.0)
        self.assertEqual(bridge._links["second"].state, "idle")
        self.assertTrue(bridge._wake.is_set())
        chat_api.calls.clear()
        engine.calls.clear()
        await bridge._reconcile()
        self.assertEqual(chat_api.calls, [("hunt", "third")])
        self.assertEqual(engine.calls, [("start", "third")])

    async def test_limited_to_idle_reconciles_without_stop(self) -> None:
        chat_api, engine = FakeChatApi(), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": "limited"})
        link = bridge._links["main"]
        await bridge._on_engine_event(link, {"type": "state", "state": "idle"})
        self.assertEqual(link.state, "idle")
        self.assertEqual(chat_api.calls, [])
        self.assertEqual(engine.calls, [])
        self.assertTrue(bridge._wake.is_set())

    async def test_snapshot_limited_with_searching_phase(self) -> None:
        chat_api, engine = FakeChatApi(phase="searching"), FakeEngine()
        bridge = make_bridge(chat_api, engine, {"main": None})
        link = bridge._links["main"]
        await bridge._on_engine_event(link, {"type": "snapshot", "state": "limited"})
        self.assertEqual(link.state, "limited")
        self.assertEqual(chat_api.calls, [("stop", "main")])
        self.assertEqual(engine.calls, [])
        self.assertTrue(bridge._wake.is_set())

    async def test_account_without_limited_until(self) -> None:
        self.assertIsNone(EngineAccount("a", True, "idle").limited_until)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"id": "old", "authorized": True, "state": "idle"},
                    {"id": "new", "authorized": True, "state": "limited", "limited_until": "2026-11-12T10:00:00"},
                ],
            )

        client = EngineClient("http://engine", "token")
        await client._http.aclose()
        client._http = httpx.AsyncClient(base_url="http://engine/api/v1", transport=httpx.MockTransport(handler))
        accounts = await client.accounts()
        await client.aclose()
        self.assertEqual([a.limited_until for a in accounts], [None, "2026-11-12T10:00:00"])
        self.assertEqual(accounts[1].state, "limited")


if __name__ == "__main__":
    unittest.main()
