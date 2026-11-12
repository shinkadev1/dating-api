"""Премиум без базы и Redis: подпись ссылки, маршрут ссылки и запрет медиа без премиума.

Запуск из chat_api/: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import premium_link
from app.config import Settings, get_settings
from app.deps import get_chat, get_media
from app.peer import Peer
from app.routers import peers
from app.schemas import MediaOut, StateOut
from app.services import chat as chat_module
from app.services.chat import MEDIA_NEEDS_PREMIUM, PARTNER_NEEDS_PREMIUM, ChatService

SECRET = "s" * 32
SERVICE = "service-token-0001"
APP = "app-token-00000002"


class TokenTests(unittest.TestCase):
    def test_roundtrip_hides_user_id(self) -> None:
        token = premium_link.sign("vk", 208891943, SECRET, now=1_000)
        self.assertNotIn("208891943", token)
        self.assertEqual(premium_link.verify(token, SECRET, now=1_000 + 3600), ("vk", 208891943))

    def test_rejects_tamper_secret_and_expiry(self) -> None:
        token = premium_link.sign("tg", 42, SECRET, now=1_000)
        flipped = token[:-3] + ("A" if token[-3] != "A" else "B") + token[-2:]
        for bad, secret, now in [
            (flipped, SECRET, 1_000),
            (token, "x" * 32, 1_000),
            (token, SECRET, 1_000 + premium_link.TTL_SECONDS),
            (token, SECRET, 1_000 - 3_600),
            ("2" + token[1:], SECRET, 1_000),
            ("", SECRET, 1_000),
            ("1!!!", SECRET, 1_000),
        ]:
            with self.subTest(bad=bad[:12], now=now), self.assertRaises(premium_link.InvalidLinkToken):
                premium_link.verify(bad, secret, now=now)

    def test_engine_and_bad_ids_cannot_be_signed(self) -> None:
        for platform, user_id in [("engine", 1), ("vk", 0)]:
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                premium_link.sign(platform, user_id, SECRET)


class FakeChat:
    def __init__(self, phase: str = "chatting", blocked: str | None = None, until: datetime | None = None, result: str = "sent"
    ) -> None:
        self.result = result
        self.phase = phase
        self.blocked = blocked
        self.until = until
        self.forwarded: list[object] = []
        self.forward_kwargs: list[dict[str, object]] = []

    async def state(self, peer: Peer) -> StateOut:
        return StateOut(phase=self.phase)  # type: ignore[arg-type]

    async def media_block_reason(self, peer: Peer, kind: str) -> str | None:
        return self.blocked

    async def premium_until(self, peer: Peer) -> datetime | None:
        return self.until

    async def forward_media(self, *args: object, **kwargs: object) -> str:
        self.forwarded.append(args)
        self.forward_kwargs.append(kwargs)
        return self.result


class FakeMedia:
    def __init__(self) -> None:
        self.saved = 0

    async def save(self, *args: object) -> object:
        self.saved += 1
        raise AssertionError("медиа без премиума не должно сохраняться")


class SavingMedia(FakeMedia):
    async def save(self, peer: Peer, kind: str, body: bytes) -> object:
        self.saved += 1
        return MediaOut(id=UUID(int=1), kind=kind, key="k", content_type="image/jpeg", size=len(body))

    async def save_locked_copy(self, peer: Peer, body: bytes) -> UUID:
        return UUID(int=2)


def make_client(chat: FakeChat, media: FakeMedia, secret: str | None = SECRET) -> TestClient:
    settings = Settings(
        database_url="postgresql://unused",
        chat_api_service_token=SERVICE,
        chat_api_app_token=APP,
        premium_link_secret=secret,
        pay_public_url="https://pay.example.test/",
    )
    app = FastAPI()
    app.include_router(peers.router, prefix="/api/v1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_chat] = lambda: chat
    app.dependency_overrides[get_media] = lambda: media
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {SERVICE}"}


class RouteTests(unittest.TestCase):
    def test_pay_link_contract(self) -> None:
        until = datetime.now(timezone.utc) + timedelta(days=3)
        client = make_client(FakeChat(until=until), FakeMedia())
        response = client.get("/api/v1/peers/vk/club1/77/premium/link", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["premium_active"])
        self.assertIsNotNone(body["premium_until"])
        url = urlsplit(body["url"])
        self.assertEqual(f"{url.scheme}://{url.netloc}", "https://pay.example.test")
        self.assertTrue(url.path.startswith("/checkout/"))
        token = url.path.removeprefix("/checkout/")
        query = parse_qs(url.query)
        self.assertEqual(query, {"utm_source": ["vk"], "utm_campaign": ["premium"]})
        self.assertNotIn("77", url.query)
        self.assertEqual(premium_link.verify(token, SECRET), ("vk", 77))

    def test_pay_link_inactive_unset_secret_and_foreign_platform(self) -> None:
        response = make_client(FakeChat(), FakeMedia()).get("/api/v1/peers/tg/bot/5/premium/link", headers=AUTH)
        self.assertEqual(response.json()["premium_active"], False)
        self.assertIsNone(response.json()["premium_until"])
        unset = make_client(FakeChat(), FakeMedia(), secret=None)
        self.assertEqual(unset.get("/api/v1/peers/vk/club1/5/premium/link", headers=AUTH).status_code, 503)
        # Токен бота не действует за пользователей сайта.
        foreign = make_client(FakeChat(), FakeMedia()).get("/api/v1/peers/web/site/5/premium/link", headers=AUTH)
        self.assertEqual(foreign.status_code, 403)

    def test_media_rejected_before_upload_is_saved(self) -> None:
        for reason in (MEDIA_NEEDS_PREMIUM, PARTNER_NEEDS_PREMIUM):
            chat, media = FakeChat(blocked=reason), FakeMedia()
            response = make_client(chat, media).post(
                "/api/v1/peers/vk/club1/77/media",
                headers=AUTH,
                data={"kind": "photo"},
                files={"file": ("a.jpg", b"\xff\xd8\xff" + b"0" * 64, "image/jpeg")},
            )
            with self.subTest(reason=reason):
                self.assertEqual(response.status_code, 403)
                self.assertIn("премиум", response.json()["detail"])
                self.assertEqual(media.saved, 0)
                self.assertEqual(chat.forwarded, [])

    def test_photo_from_sender_without_premium_passes_with_locked_copy(self) -> None:
        chat, media = FakeChat(), SavingMedia()
        response = make_client(chat, media).post(
            "/api/v1/peers/vk/club1/77/media",
            headers=AUTH,
            data={"kind": "photo"},
            files={"file": ("a.jpg", b"\xff\xd8\xff" + b"0" * 64, "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(UUID(int=1)))
        self.assertEqual(len(chat.forwarded), 1)
        locked_copy = chat.forward_kwargs[0]["locked_copy"]
        self.assertEqual(asyncio.run(locked_copy()), UUID(int=2))  # type: ignore[operator]

    def test_file_id_photo_locked_for_partner_is_204_for_sender(self) -> None:
        for result, code in (("locked", 204), ("sent", 204), ("skipped", 413), ("left", 409)):
            chat = FakeChat(result=result)
            response = make_client(chat, FakeMedia()).post(
                "/api/v1/peers/tg/bot/77/media",
                headers=AUTH,
                data={"kind": "photo", "tg_file_id": "FILE", "tg_method": "photo"},
            )
            with self.subTest(result=result):
                self.assertEqual(response.status_code, code)
                self.assertEqual(len(chat.forwarded), 1)
                if code == 204:
                    self.assertEqual(response.content, b"")

    def test_media_not_chatting_is_still_409(self) -> None:
        response = make_client(FakeChat(phase="idle", blocked=MEDIA_NEEDS_PREMIUM), FakeMedia()).post(
            "/api/v1/peers/vk/club1/77/media", headers=AUTH, data={"kind": "photo"}
        )
        self.assertEqual(response.status_code, 409)


class FakePool:
    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield object()


class GateTests(unittest.TestCase):
    me = Peer("vk", "club1", 1)

    def reason(self, premium: set[Peer], partner: Peer | None, kind: str = "video") -> str | None:
        service = ChatService.__new__(ChatService)
        service._pool = FakePool()  # type: ignore[attr-defined]

        async def is_premium(conn: object, peer: Peer) -> bool:
            return peer in premium and not peer.is_engine

        async def get_session(conn: object, peer: Peer) -> object:
            return SimpleNamespace(phase="chatting", partner=partner)

        async def linked(conn: object, peer: Peer, session: object) -> Peer | None:
            return partner

        with (
            patch.object(chat_module.repo, "user_is_premium", is_premium),
            patch.object(chat_module.repo, "get_session", get_session),
            patch.object(ChatService, "_linked_partner", staticmethod(linked)),
        ):
            return asyncio.run(service.media_block_reason(self.me, kind))

    def test_both_premium_required_engine_exempt(self) -> None:
        human = Peer("tg", "bot", 2)
        engine = Peer("engine", "acc", 1)
        self.assertEqual(self.reason(set(), human), MEDIA_NEEDS_PREMIUM)
        self.assertEqual(self.reason({self.me}, human), PARTNER_NEEDS_PREMIUM)
        self.assertIsNone(self.reason({self.me, human}, human))
        self.assertIsNone(self.reason({self.me}, engine))
        self.assertIsNone(self.reason({self.me}, None))

    def test_photo_is_not_gated(self) -> None:
        human = Peer("tg", "bot", 2)
        for premium in (set(), {self.me}, {human}):
            with self.subTest(premium=len(premium)):
                self.assertIsNone(self.reason(premium, human, "photo"))
        for kind in ("video", "video_note", "voice"):
            with self.subTest(kind=kind):
                self.assertEqual(self.reason(set(), human, kind), MEDIA_NEEDS_PREMIUM)
                self.assertEqual(self.reason({self.me}, human, kind), PARTNER_NEEDS_PREMIUM)

    def test_engine_sender_is_not_gated(self) -> None:
        self.me = Peer("engine", "acc", 1)
        self.assertIsNone(self.reason(set(), Peer("vk", "club1", 3)))


if __name__ == "__main__":
    unittest.main()
