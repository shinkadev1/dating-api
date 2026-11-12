"""История диалогов без базы: открытие и закрытие диалога, запись реплик после доставки.

Запуск из chat_api/: python -m pytest tests
"""

from __future__ import annotations

import logging
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from app.peer import Peer
from app.schemas import ChatEvent
from app.repositories import sessions as sessions_repo
from app.services import chat as chat_module
from app.services.chat import ChatService, NotChattingError

A = Peer("vk", "club1", 11)
B = Peer("tg", "bot", 22)
ENGINE = Peer("engine", "acc1", 1)


class FakeConn:
    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self


class FakePool:
    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield FakeConn()


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Peer, object]] = []
        self.engine_requests = 0

    async def publish(self, peer: Peer, event: object) -> None:
        self.published.append((peer, event))

    async def request_engine(self) -> None:
        self.engine_requests += 1


class Repo:
    """Подмена репозитория сессий: запоминает вызовы и отдаёт заданные сессии."""

    def __init__(self, sessions: dict[Peer, sessions_repo.SessionRow] | None = None) -> None:
        self.sessions = sessions or {}
        self.calls: list[tuple] = []
        self.opened = 0
        self.searcher: Peer | None = None
        self.with_engine: tuple[Peer, Peer] | None = None
        self.waiting: Peer | None = None
        # Что вернёт set_idle/set_searching для peer (один раз): закрытые диалоги со счётчиками.
        self.ends: dict[Peer, list[sessions_repo.DialogEnd]] = {}
        self.premium: set[Peer] = set()

    async def user_is_premium(self, conn: object, peer: Peer) -> bool:
        return peer in self.premium

    async def lock_matchmaking(self, conn: object) -> None:
        pass

    async def get_session(self, conn: object, peer: Peer):  # type: ignore[no-untyped-def]
        return self.sessions.get(peer)

    async def set_idle(
        self, conn: object, peer: Peer, *, reason: str, ended_by: Peer | None
    ) -> list[sessions_repo.DialogEnd]:
        self.calls.append(("idle", peer, reason, ended_by))
        return self.ends.pop(peer, [])

    async def set_searching(self, conn: object, peer: Peer, mode: str = "normal") -> list[sessions_repo.DialogEnd]:
        self.calls.append(("searching", peer))
        return self.ends.pop(peer, [])

    async def record_search(self, conn: object, peer: Peer) -> None:
        pass

    async def find_searcher(self, conn: object, peer: Peer, mode: str = "normal") -> Peer | None:
        return self.searcher

    async def find_human_with_engine(self, conn: object, mode: str = "normal"):  # type: ignore[no-untyped-def]
        return self.with_engine

    async def oldest_waiting_human(self, conn: object, wait: float, mode: str = "normal") -> Peer | None:
        return self.waiting

    async def open_dialog(self, conn: object, a: Peer, b: Peer) -> int:
        self.opened += 1
        self.calls.append(("open", a, b))
        return 100 + self.opened

    async def set_chatting(self, conn: object, peer: Peer, partner: Peer, dialog_id: int) -> None:
        self.calls.append(("chatting", peer, partner, dialog_id))


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.fail = fail

    async def insert_message(self, conn: object, **kwargs: object) -> bool:
        if self.fail:
            raise RuntimeError("секретный текст не должен попасть в лог")
        self.rows.append(kwargs)
        return True


def make_service() -> tuple[ChatService, FakeEvents]:
    service = ChatService.__new__(ChatService)
    events = FakeEvents()
    service._pool = FakePool()  # type: ignore[attr-defined]
    service._events = events  # type: ignore[attr-defined]
    service._engine_enabled = True  # type: ignore[attr-defined]
    service._fallback_after = 0.0  # type: ignore[attr-defined]
    return service, events


def chatting(partner: Peer, dialog_id: int | None = 5) -> sessions_repo.SessionRow:
    return sessions_repo.SessionRow(phase="chatting", partner=partner, dialog_id=dialog_id)


def idle() -> sessions_repo.SessionRow:
    return sessions_repo.SessionRow(phase="idle", partner=None)


async def run_with(repo: Repo, rec: Recorder, call):  # type: ignore[no-untyped-def]
    service, events = make_service()
    with (
        patch.object(chat_module, "repo", repo),
        patch.object(chat_module, "messages_repo", rec),
    ):
        result = await call(service)
    return result, events


class DialogLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_pair_opens_one_dialog(self) -> None:
        repo = Repo()
        repo.searcher = B
        await run_with(repo, Recorder(), lambda s: s.search(A))
        self.assertEqual(repo.opened, 1)
        self.assertIn(("open", A, B), repo.calls)
        self.assertIn(("chatting", A, B, 101), repo.calls)
        self.assertIn(("chatting", B, A, 101), repo.calls)

    async def test_search_without_partner_opens_nothing(self) -> None:
        repo = Repo()
        state, _ = await run_with(repo, Recorder(), lambda s: s.search(A))
        self.assertEqual(state.phase, "searching")
        self.assertEqual(repo.opened, 0)

    async def test_stolen_human_closes_engine_dialog_and_opens_new(self) -> None:
        repo = Repo()
        repo.with_engine = (B, ENGINE)
        await run_with(repo, Recorder(), lambda s: s.search(A))
        self.assertIn(("idle", ENGINE, "stopped", None), repo.calls)
        self.assertIn(("open", A, B), repo.calls)
        self.assertEqual(repo.opened, 1)

    async def test_engine_attach_opens_dialog_with_engine_as_a(self) -> None:
        repo = Repo({ENGINE: sessions_repo.SessionRow(phase="searching", partner=None)})
        repo.waiting = A
        result, _ = await run_with(repo, Recorder(), lambda s: s.engine_attach(ENGINE))
        self.assertTrue(result.attached)
        self.assertEqual(repo.opened, 1)
        self.assertIn(("open", ENGINE, A), repo.calls)
        self.assertIn(("chatting", ENGINE, A, 101), repo.calls)
        self.assertIn(("chatting", A, ENGINE, 101), repo.calls)

    async def test_engine_attach_refused_closes_without_dialog(self) -> None:
        repo = Repo({ENGINE: sessions_repo.SessionRow(phase="searching", partner=None)})
        result, _ = await run_with(repo, Recorder(), lambda s: s.engine_attach(ENGINE))
        self.assertFalse(result.attached)
        self.assertEqual(repo.opened, 0)
        self.assertIn(("idle", ENGINE, "stopped", None), repo.calls)

    async def test_stop_closes_both_with_ended_by_peer(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertIn(("idle", A, "stopped", A), repo.calls)
        self.assertIn(("idle", B, "stopped", A), repo.calls)

    async def test_report_is_reported_reason(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        await run_with(repo, Recorder(), lambda s: s.report(A))
        self.assertIn(("idle", A, "reported", A), repo.calls)
        self.assertIn(("idle", B, "reported", A), repo.calls)
        self.assertIn(("searching", A), repo.calls)

    async def test_undeliverable_reason(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        await run_with(repo, Recorder(), lambda s: s.delivery_failed(A))
        self.assertIn(("idle", A, "undeliverable", A), repo.calls)
        self.assertIn(("idle", B, "undeliverable", A), repo.calls)


def ends(*pairs: tuple[Peer, int]) -> list[sessions_repo.DialogEnd]:
    return [sessions_repo.DialogEnd(peer, count) for peer, count in pairs]


def left_events(events: FakeEvents) -> list[tuple[Peer, ChatEvent]]:
    return [(peer, event) for peer, event in events.published if getattr(event, "type", None) == "partner_left"]


def promo_flagged(events: FakeEvents) -> list[Peer]:
    return [peer for peer, event in left_events(events) if getattr(event, "promo", None)]


def no_promo_events(events: FakeEvents) -> bool:
    return all(getattr(event, "type", None) != "promo" for _, event in events.published)


class PromoTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_at_threshold_flags_partner_and_initiator(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 5), (B, 5))
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        peer, event = events.published[0]
        self.assertEqual((peer, event.type), (B, "partner_left"))  # type: ignore[attr-defined]
        self.assertTrue(event.promo)  # type: ignore[attr-defined]
        self.assertTrue(result.promo)
        self.assertTrue(no_promo_events(events))

    async def test_thresholds_every_ten(self) -> None:
        for n in (5, 15, 25, 35):
            repo = Repo({A: chatting(B), B: chatting(A)})
            repo.ends[A] = ends((A, n), (B, n))
            result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
            self.assertEqual(promo_flagged(events), [B], n)
            self.assertTrue(result.promo, n)
        for n in (4, 6, 10, 14, 16, 20):
            repo = Repo({A: chatting(B), B: chatting(A)})
            repo.ends[A] = ends((A, n), (B, n))
            result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
            self.assertEqual(promo_flagged(events), [], n)
            self.assertFalse(result.promo, n)

    async def test_only_side_at_threshold_gets_flag(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 4), (B, 15))
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertEqual(promo_flagged(events), [B])
        self.assertFalse(result.promo)

    async def test_premium_gets_no_promo(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 5), (B, 5))
        repo.premium = {A}
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertEqual(promo_flagged(events), [B])
        self.assertFalse(result.promo)
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 5), (B, 5))
        repo.premium = {B}
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertEqual(promo_flagged(events), [])
        self.assertTrue(result.promo)

    async def test_web_and_engine_get_no_promo(self) -> None:
        web = Peer("web", "site", 5)
        repo = Repo({A: chatting(web), web: chatting(A)})
        repo.ends[A] = ends((A, 4), (web, 5))
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertEqual(promo_flagged(events), [])
        self.assertFalse(result.promo)
        repo = Repo({A: chatting(ENGINE), ENGINE: chatting(A)})
        repo.ends[A] = ends((A, 4), (ENGINE, 5))
        _, events = await run_with(repo, Recorder(), lambda s: s.stop(A))
        self.assertEqual(promo_flagged(events), [])
        repo = Repo({web: chatting(B), B: chatting(web)})
        repo.ends[web] = ends((web, 5), (B, 4))
        result, events = await run_with(repo, Recorder(), lambda s: s.stop(web))
        self.assertEqual(promo_flagged(events), [])
        self.assertFalse(result.promo)

    async def test_search_flags_partner_not_initiator(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 5), (B, 5))
        state, events = await run_with(repo, Recorder(), lambda s: s.search(A))
        self.assertEqual(promo_flagged(events), [B])
        self.assertEqual([p for p, _ in events.published if p == A], [])
        self.assertEqual(state.phase, "searching")
        self.assertTrue(no_promo_events(events))

    async def test_stolen_from_engine_gets_no_event(self) -> None:
        repo = Repo()
        repo.with_engine = (B, ENGINE)
        repo.ends[ENGINE] = ends((B, 5))
        _, events = await run_with(repo, Recorder(), lambda s: s.search(A))
        self.assertEqual([p for p, _ in events.published if p == B], [])
        self.assertEqual(promo_flagged(events), [])

    async def test_delivery_failed_has_no_promo(self) -> None:
        repo = Repo({A: chatting(B), B: chatting(A)})
        repo.ends[A] = ends((A, 5), (B, 5))
        _, events = await run_with(repo, Recorder(), lambda s: s.delivery_failed(A))
        self.assertEqual([p for p, _ in left_events(events)], [B])
        self.assertEqual(promo_flagged(events), [])

    async def test_engine_hunt_flags_human(self) -> None:
        repo = Repo({ENGINE: chatting(A), A: chatting(ENGINE)})
        repo.ends[ENGINE] = ends((A, 5), (ENGINE, 5))
        _, events = await run_with(repo, Recorder(), lambda s: s.engine_hunt(ENGINE))
        self.assertEqual(promo_flagged(events), [A])

    async def test_send_message_on_broken_pair_flags_partner(self) -> None:
        repo = Repo({A: chatting(B), B: idle()})
        repo.ends[A] = ends((A, 5), (B, 5))
        result, events = await run_with(repo, Recorder(), lambda s: s.send_message(A, "привет"))
        self.assertFalse(result.delivered)
        self.assertEqual(promo_flagged(events), [B])

    async def test_event_schema(self) -> None:
        self.assertNotIn("promo", ChatEvent(type="partner_left", reason="stopped").model_dump(exclude_none=True))
        self.assertTrue(ChatEvent(type="partner_left", reason="stopped", promo=True).model_dump(exclude_none=True)["promo"])
        with self.assertRaises(ValidationError):
            ChatEvent(type="promo")  # type: ignore[arg-type]


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_recorded_after_publish(self) -> None:
        repo = Repo({A: chatting(B, 7), B: chatting(A, 7)})
        rec = Recorder()
        result, events = await run_with(repo, rec, lambda s: s.send_message(A, "привет"))
        self.assertTrue(result.delivered)
        self.assertEqual(len(events.published), 1)
        self.assertEqual(len(rec.rows), 1)
        row = rec.rows[0]
        self.assertEqual(
            (row["dialog_id"], row["sender"], row["partner"], row["kind"], row["status"], row["text"]),
            (7, A, B, "text", "delivered", "привет"),
        )

    async def test_record_failure_keeps_delivery_and_logs_no_text(self) -> None:
        repo = Repo({A: chatting(B, 7), B: chatting(A, 7)})
        with self.assertLogs(chat_module.logger, level=logging.WARNING) as logs:
            result, events = await run_with(repo, Recorder(fail=True), lambda s: s.send_message(A, "тайный текст"))
        self.assertTrue(result.delivered)
        self.assertEqual(len(events.published), 1)
        output = "\n".join(logs.output)
        self.assertIn("Не записал сообщение в диалог 7", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("тайный текст", output)
        self.assertNotIn("секретный", output)

    async def test_no_dialog_id_writes_nothing(self) -> None:
        repo = Repo({A: chatting(B, None), B: chatting(A, None)})
        rec = Recorder()
        result, _ = await run_with(repo, rec, lambda s: s.send_message(A, "x"))
        self.assertTrue(result.delivered)
        self.assertEqual(rec.rows, [])

    async def test_text_without_link_is_left(self) -> None:
        repo = Repo({A: chatting(B, 7), B: idle()})
        rec = Recorder()
        result, events = await run_with(repo, rec, lambda s: s.send_message(A, "поздно"))
        self.assertFalse(result.delivered)
        self.assertEqual([r["status"] for r in rec.rows], ["left"])
        self.assertEqual(rec.rows[0]["partner"], B)

    async def test_text_not_chatting_raises_and_writes_nothing(self) -> None:
        repo = Repo({A: idle()})
        rec = Recorder()
        with self.assertRaises(NotChattingError):
            await run_with(repo, rec, lambda s: s.send_message(A, "x"))
        self.assertEqual(rec.rows, [])

    async def media(self, repo: Repo, rec: Recorder, media_id=None, kind="photo", content_type=None, **extra):  # type: ignore[no-untyped-def]
        result, _ = await run_with(repo, rec, lambda s: s.forward_media(A, media_id, kind, content_type, **extra))
        return result

    async def test_media_sent_recorded(self) -> None:
        repo = Repo({A: chatting(B, 7), B: chatting(A, 7)})
        rec = Recorder()
        media_id = uuid4()
        result = await self.media(repo, rec, media_id=media_id, kind="video", content_type="video/mp4")
        self.assertEqual(result, "sent")
        row = rec.rows[0]
        self.assertEqual(
            (row["dialog_id"], row["kind"], row["status"], row["media_id"], row["content_type"]),
            (7, "video", "delivered", media_id, "video/mp4"),
        )

    async def test_tg_audio_is_audio_kind_with_file_id_only(self) -> None:
        repo = Repo({A: chatting(B, 7), B: chatting(A, 7)})
        rec = Recorder()
        result = await self.media(repo, rec, kind="voice", tg_file_id="FILE", tg_method="audio")
        self.assertEqual(result, "sent")
        row = rec.rows[0]
        self.assertEqual(
            (row["kind"], row["tg_method"], row["tg_file_id"], row["media_id"]),
            ("audio", "audio", "FILE", None),
        )

    async def test_media_skipped_recorded(self) -> None:
        other = Peer("vk", "club1", 33)
        repo = Repo({A: chatting(other, 7), other: chatting(A, 7)})
        rec = Recorder()
        result = await self.media(repo, rec, tg_file_id="FILE", tg_method="photo")
        self.assertEqual(result, "skipped")
        self.assertEqual([r["status"] for r in rec.rows], ["skipped"])

    async def test_media_left_when_pair_gone_recorded(self) -> None:
        repo = Repo({A: chatting(B, 7), B: idle()})
        rec = Recorder()
        result = await self.media(repo, rec, tg_file_id="FILE", tg_method="photo")
        self.assertEqual(result, "left")
        self.assertEqual([r["status"] for r in rec.rows], ["left"])

    async def test_media_left_when_not_chatting_not_recorded(self) -> None:
        repo = Repo({A: idle()})
        rec = Recorder()
        result = await self.media(repo, rec, tg_file_id="FILE", tg_method="photo")
        self.assertEqual(result, "left")
        self.assertEqual(rec.rows, [])

    async def test_media_without_dialog_id_writes_nothing(self) -> None:
        repo = Repo({A: chatting(B, None), B: chatting(A, None)})
        rec = Recorder()
        result = await self.media(repo, rec, tg_file_id="FILE", tg_method="photo")
        self.assertEqual(result, "sent")
        self.assertEqual(rec.rows, [])


if __name__ == "__main__":
    unittest.main()
