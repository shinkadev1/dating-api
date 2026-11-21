"""Журнал инцидентов поиска без базы: определение причины, запись при стопе и смене режима, маршрут статуса.

Запуск из chat_api/: python -m pytest tests/test_search_incidents.py
"""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.deps import get_chat
from app.peer import Peer
from app.repositories import sessions as sessions_repo
from app.routers import engine as engine_router
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.services.incidents import AccountInfo, account_from_row, build_snapshot, classify_incident

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(seconds=60)
A = Peer("vk", "club1", 11)
B = Peer("tg", "bot", 22)
ENGINE = Peer("engine", "acc1", 1)


def acc(id_: str = "a", mode: str = "normal", state: str = "idle", **kw) -> AccountInfo:  # type: ignore[no-untyped-def]
    return AccountInfo(id_, mode, state, **kw)


def classify(accounts, mode="normal", wait=60, since=SINCE):  # type: ignore[no-untyped-def]
    return classify_incident(wait_seconds=wait, mode=mode, searching_since=since, accounts=accounts, now=NOW)


class ClassifyTests(unittest.TestCase):
    def test_left_quickly_wins_over_everything(self) -> None:
        self.assertEqual(classify([], wait=9), "left_quickly")
        self.assertEqual(classify([acc()], wait=0), "left_quickly")
        self.assertNotEqual(classify([], wait=10), "left_quickly")

    def test_no_accounts_at_all(self) -> None:
        self.assertEqual(classify([]), "no_account_for_mode")

    def test_no_account_serves_mode(self) -> None:
        self.assertEqual(classify([acc(mode="normal")], mode="adult"), "no_account_for_mode")
        self.assertEqual(classify([acc(mode="adult")], mode="normal"), "no_account_for_mode")

    def test_any_serves_both_modes(self) -> None:
        for mode in ("normal", "adult"):
            self.assertEqual(classify([acc(mode="any")], mode=mode), "bridge_not_hunting")

    def test_all_unavailable_recent_error(self) -> None:
        recent = NOW - timedelta(minutes=2)
        accounts = [
            acc("a", last_error="409 all bots unavailable", last_error_at=recent),
            acc("b", last_error="cooldown", last_error_at=recent),
        ]
        self.assertEqual(classify(accounts), "all_accounts_unavailable")

    def test_limited_is_unavailable_even_if_error_is_old(self) -> None:
        old = NOW - timedelta(hours=5)
        old = NOW - timedelta(hours=5)
        self.assertEqual(classify([acc(last_error="limited", last_error_at=old)]), "all_accounts_unavailable")

    def test_limited_older_than_six_hours_is_not_unavailable(self) -> None:
        stale = NOW - timedelta(hours=7)
        self.assertEqual(classify([acc(last_error="limited", last_error_at=stale)]), "bridge_not_hunting")

    def test_searching_account_counts_only_for_its_hunt_mode(self) -> None:
        before = SINCE - timedelta(minutes=5)
        a = acc(mode="any", state="searching", hunt_mode="adult", last_hunt_normal_at=before, last_hunt_adult_at=before)
        self.assertEqual(classify([a], mode="normal"), "bridge_not_hunting")
        self.assertEqual(classify([a], mode="adult"), "engine_searched_no_match")

    def test_old_plain_error_is_not_unavailable(self) -> None:
        old = NOW - timedelta(hours=1)
        self.assertEqual(classify([acc(last_error="409", last_error_at=old)]), "bridge_not_hunting")

    def test_one_healthy_account_breaks_unavailable(self) -> None:
        recent = NOW - timedelta(minutes=1)
        accounts = [acc("a", last_error="409", last_error_at=recent), acc("b")]
        self.assertEqual(classify(accounts), "bridge_not_hunting")

    def test_other_mode_accounts_do_not_count_for_unavailable(self) -> None:
        recent = NOW - timedelta(minutes=1)
        accounts = [acc("a", mode="normal"), acc("b", mode="adult", last_error="limited", last_error_at=recent)]
        self.assertEqual(classify(accounts, mode="normal"), "bridge_not_hunting")
        self.assertEqual(classify(accounts, mode="adult"), "all_accounts_unavailable")

    def test_engine_searched_after_wait_started(self) -> None:
        hunted = acc(last_hunt_normal_at=SINCE + timedelta(seconds=20))
        self.assertEqual(classify([hunted]), "engine_searched_no_match")

    def test_engine_hunt_in_other_mode_does_not_count(self) -> None:
        hunted = acc(mode="any", last_hunt_adult_at=SINCE + timedelta(seconds=20))
        self.assertEqual(classify([hunted], mode="normal"), "bridge_not_hunting")
        self.assertEqual(classify([hunted], mode="adult"), "engine_searched_no_match")

    def test_hunt_before_wait_does_not_count_unless_still_searching(self) -> None:
        before = SINCE - timedelta(minutes=5)
        self.assertEqual(classify([acc(last_hunt_normal_at=before)]), "bridge_not_hunting")
        self.assertEqual(classify([acc(state="searching", hunt_mode="normal", last_hunt_normal_at=before)]), "engine_searched_no_match")

    def test_bridge_not_hunting_fallback(self) -> None:
        self.assertEqual(classify([acc()]), "bridge_not_hunting")

    def test_unavailable_beats_searched(self) -> None:
        recent = NOW - timedelta(minutes=1)
        a = acc(last_error="409", last_error_at=recent, last_hunt_normal_at=SINCE + timedelta(seconds=5))
        self.assertEqual(classify([a]), "all_accounts_unavailable")

    def test_account_from_row_defaults_unknown_mode_to_normal(self) -> None:
        self.assertEqual(account_from_row({"bot_key": "x", "mode": None, "state": "idle"}).mode, "normal")
        self.assertEqual(account_from_row({"bot_key": "x", "mode": "any", "state": None}).mode, "any")

    def test_snapshot_shape(self) -> None:
        queue = {"waiting": {"normal": 2, "adult": 0}, "hunting": {"normal": 1, "adult": 0}}
        snap = build_snapshot([acc("a", last_error="409", last_error_at=NOW)], queue)
        self.assertEqual(snap["waiting"], {"normal": 2, "adult": 0})
        self.assertEqual(snap["hunting"], {"normal": 1, "adult": 0})
        self.assertEqual(
            snap["accounts"],
            [{"id": "a", "mode": "normal", "state": "idle", "last_error": "409", "last_error_at": NOW.isoformat()}],
        )


class FakeConn:
    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self


class FakePool:
    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield FakeConn()


class FakeEvents:
    async def publish(self, peer: Peer, event: object) -> None:
        pass

    async def request_engine(self) -> None:
        pass


class FakeSessions:
    def __init__(self, sessions: dict[Peer, sessions_repo.SessionRow]) -> None:
        self.sessions = sessions

    async def lock_matchmaking(self, conn: object) -> None:
        pass

    async def get_session(self, conn: object, peer: Peer):  # type: ignore[no-untyped-def]
        return self.sessions.get(peer)

    async def set_idle(self, conn: object, peer: Peer, *, reason: str, ended_by: Peer | None):  # type: ignore[no-untyped-def]
        return []

    async def set_searching(self, conn: object, peer: Peer, mode: str = "normal"):  # type: ignore[no-untyped-def]
        return []

    async def record_search(self, conn: object, peer: Peer) -> None:
        pass

    async def find_searcher(self, conn: object, peer: Peer, mode: str = "normal") -> None:
        return None

    async def find_human_with_engine(self, conn: object, mode: str = "normal") -> None:
        return None

    async def user_is_premium(self, conn: object, peer: Peer) -> bool:
        return True


class FakeIncidents:
    def __init__(self, fail: bool = False) -> None:
        self.inserted: list[dict] = []
        self.fail = fail
        self.accounts: list[dict] = []

    async def engine_accounts(self, conn: object, window: float):  # type: ignore[no-untyped-def]
        return self.accounts

    async def queue_by_mode(self, conn: object):  # type: ignore[no-untyped-def]
        return {"waiting": {"normal": 1, "adult": 0}, "hunting": {"normal": 0, "adult": 0}}

    async def insert_incident(self, conn: object, peer: Peer, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("база недоступна")
        self.inserted.append({"peer": peer, **kwargs})

    async def mark_engine_hunt(self, conn: object, bot_key: str, mode: str) -> None:
        pass


def searching(mode: str = "normal", waited: float = 60) -> sessions_repo.SessionRow:
    since = datetime.now(timezone.utc) - timedelta(seconds=waited)
    return sessions_repo.SessionRow(phase="searching", partner=None, mode=mode, searching_since=since)


def chatting_pair(mode: str = "normal") -> dict[Peer, sessions_repo.SessionRow]:
    return {
        A: sessions_repo.SessionRow(phase="chatting", partner=B, dialog_id=5, mode=mode),
        B: sessions_repo.SessionRow(phase="chatting", partner=A, dialog_id=5, mode=mode),
    }


async def run(sessions, incidents, call):  # type: ignore[no-untyped-def]
    service = ChatService.__new__(ChatService)
    service._pool = FakePool()  # type: ignore[attr-defined]
    service._events = FakeEvents()  # type: ignore[attr-defined]
    service._engine_enabled = False  # type: ignore[attr-defined]
    service._fallback_after = 0.0  # type: ignore[attr-defined]
    with (
        patch.object(chat_module, "repo", FakeSessions(sessions)),
        patch.object(chat_module, "incidents_repo", incidents),
    ):
        result = await call(service)
        await service.wait_incidents()  # запись журнала идёт фоном
        return result


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_while_searching_records_incident(self) -> None:
        inc = FakeIncidents()
        await run({A: searching()}, inc, lambda s: s.stop(A))
        self.assertEqual(len(inc.inserted), 1)
        row = inc.inserted[0]
        self.assertEqual((row["peer"], row["mode"], row["outcome"]), (A, "normal", "stopped"))
        self.assertEqual(row["reason"], "no_account_for_mode")
        self.assertGreaterEqual(row["wait_seconds"], 59)
        self.assertEqual(row["snapshot"]["waiting"]["normal"], 1)

    async def test_quick_stop_is_left_quickly_without_snapshot(self) -> None:
        inc = FakeIncidents()
        await run({A: searching(waited=2)}, inc, lambda s: s.stop(A))
        self.assertEqual(inc.inserted[0]["reason"], "left_quickly")
        self.assertIsNone(inc.inserted[0]["snapshot"])

    async def test_reason_uses_account_rows(self) -> None:
        inc = FakeIncidents()
        inc.accounts = [
            {
                "bot_key": "acc1",
                "mode": "any",
                "state": "idle",
                "last_error": "limited",
                "last_error_at": datetime.now(timezone.utc),
            }
        ]
        await run({A: searching("adult")}, inc, lambda s: s.stop(A))
        self.assertEqual(inc.inserted[0]["reason"], "all_accounts_unavailable")
        self.assertEqual(inc.inserted[0]["snapshot"]["accounts"][0]["id"], "acc1")

    async def test_stop_from_dialog_records_nothing(self) -> None:
        inc = FakeIncidents()
        await run(chatting_pair(), inc, lambda s: s.stop(A))
        self.assertEqual(inc.inserted, [])

    async def test_stop_while_idle_records_nothing(self) -> None:
        inc = FakeIncidents()
        await run({A: sessions_repo.SessionRow(phase="idle", partner=None)}, inc, lambda s: s.stop(A))
        await run({}, inc, lambda s: s.stop(A))
        self.assertEqual(inc.inserted, [])

    async def test_mode_change_records_incident(self) -> None:
        inc = FakeIncidents()
        await run({A: searching("normal")}, inc, lambda s: s.search(A, "adult"))
        self.assertEqual(len(inc.inserted), 1)
        self.assertEqual((inc.inserted[0]["outcome"], inc.inserted[0]["mode"]), ("mode_changed", "normal"))

    async def test_repeat_search_same_mode_is_not_incident(self) -> None:
        inc = FakeIncidents()
        await run({A: searching("normal")}, inc, lambda s: s.search(A, "normal"))
        self.assertEqual(inc.inserted, [])

    async def test_search_from_dialog_records_nothing(self) -> None:
        inc = FakeIncidents()
        await run(chatting_pair("adult"), inc, lambda s: s.search(A, "normal"))
        self.assertEqual(inc.inserted, [])

    async def test_engine_account_restart_is_not_incident(self) -> None:
        inc = FakeIncidents()
        await run({ENGINE: searching()}, inc, lambda s: s.engine_hunt(ENGINE, "normal"))
        self.assertEqual(inc.inserted, [])

    async def test_incident_is_written_in_background_task(self) -> None:
        inc = FakeIncidents()
        service = ChatService.__new__(ChatService)
        service._pool = FakePool()  # type: ignore[attr-defined]
        service._events = FakeEvents()  # type: ignore[attr-defined]
        service._engine_enabled = False  # type: ignore[attr-defined]
        service._fallback_after = 0.0  # type: ignore[attr-defined]
        with patch.object(chat_module, "repo", FakeSessions({A: searching()})), patch.object(chat_module, "incidents_repo", inc):
            await service.stop(A)
            self.assertEqual(inc.inserted, [])  # ещё не успела: stop не ждёт запись
            await service.wait_incidents()
        self.assertEqual(len(inc.inserted), 1)
        self.assertEqual(service._incident_tasks, set())

    async def test_journal_failure_does_not_break_stop(self) -> None:
        inc = FakeIncidents(fail=True)
        with self.assertLogs("app.services.chat", level="WARNING") as logs:
            result = await run({A: searching()}, inc, lambda s: s.stop(A))
        self.assertTrue(result.was_active)
        self.assertIn("Не записал инцидент", logs.output[0])

    async def test_journal_failure_does_not_break_search(self) -> None:
        inc = FakeIncidents(fail=True)
        with self.assertLogs("app.services.chat", level="WARNING"):
            state = await run({A: searching("normal")}, inc, lambda s: s.search(A, "adult"))
        self.assertEqual(state.phase, "searching")


ENGINE_TOKEN = "engine-token-000003"
SERVICE = "service-token-0001"


class FakeChat:
    def __init__(self) -> None:
        self.statuses: list[tuple] = []

    async def engine_status(self, engine: Peer, mode: str | None, error: str | None, given: bool) -> None:
        self.statuses.append((engine.bot_key, mode, error, given))


def client_for(chat: FakeChat) -> TestClient:
    settings = Settings(
        database_url="postgresql://unused",
        chat_api_service_token=SERVICE,
        chat_api_app_token="app-token-00000002",
        chat_api_engine_token=ENGINE_TOKEN,
    )
    app = FastAPI()
    app.include_router(engine_router.router, prefix="/api/v1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_chat] = lambda: chat
    return TestClient(app)


class StatusRouteTests(unittest.TestCase):
    URL = "/api/v1/engine/acc1/status"
    AUTH = {"Authorization": f"Bearer {ENGINE_TOKEN}"}

    def test_error_and_mode_passed(self) -> None:
        chat = FakeChat()
        r = client_for(chat).post(self.URL, headers=self.AUTH, json={"mode": "any", "error": "409 busy"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(chat.statuses, [("acc1", "any", "409 busy", True)])

    def test_null_error_clears_and_missing_error_keeps(self) -> None:
        chat = FakeChat()
        client = client_for(chat)
        client.post(self.URL, headers=self.AUTH, json={"error": None})
        client.post(self.URL, headers=self.AUTH, json={"mode": "adult"})
        self.assertEqual(chat.statuses, [("acc1", None, None, True), ("acc1", "adult", None, False)])

    def test_requires_engine_token_and_valid_mode(self) -> None:
        client = client_for(FakeChat())
        self.assertEqual(client.post(self.URL, json={}).status_code, 401)
        self.assertEqual(
            client.post(self.URL, headers={"Authorization": f"Bearer {SERVICE}"}, json={}).status_code, 403
        )
        self.assertEqual(client.post(self.URL, headers=self.AUTH, json={"mode": "spicy"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
