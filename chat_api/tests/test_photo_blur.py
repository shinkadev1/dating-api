"""Размытая копия фото для получателя без премиума: blur_photo и ветка forward_media.

Без базы и Redis. Запуск из chat_api/: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from PIL import Image

from app.media_blur import blur_photo
from app.peer import Peer
from app.services import chat as chat_module
from app.services.chat import ChatService

ORIGINAL = UUID(int=1)
COPY = UUID(int=2)


def make_jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (1200, 900))
    for x in range(0, 1200, 40):
        for y in range(0, 900, 40):
            img.paste(((x * 7) % 256, (y * 5) % 256, (x + y) % 256), (x, y, x + 40, y + 40))
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif)
    return out.getvalue()


class BlurTests(unittest.TestCase):
    def test_jpeg_with_exif_becomes_small_jpeg_without_exif(self) -> None:
        source = make_jpeg_with_exif()
        self.assertIn(0x010F, Image.open(io.BytesIO(source)).getexif())
        result = blur_photo(source)
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.format, "JPEG")
        self.assertLessEqual(max(img.size), 480)
        self.assertEqual(img.size, (480, 360))
        self.assertEqual(len(img.getexif()), 0)
        self.assertNotIn(b"SecretCamera", result)

    def test_png_rgba_becomes_jpeg(self) -> None:
        out = io.BytesIO()
        Image.new("RGBA", (300, 700), (10, 200, 30, 128)).save(out, format="PNG")
        img = Image.open(io.BytesIO(blur_photo(out.getvalue())))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.mode, "RGB")
        self.assertLessEqual(max(img.size), 480)

    def test_details_are_destroyed(self) -> None:
        stripes = Image.new("RGB", (400, 400), "white")
        for x in range(0, 400, 4):
            stripes.paste("black", (x, 0, x + 2, 400))
        out = io.BytesIO()
        stripes.save(out, format="PNG")
        blurred = Image.open(io.BytesIO(blur_photo(out.getvalue()))).convert("L")
        low, high = blurred.getextrema()
        self.assertLess(high - low, 40)

    def test_garbage_raises(self) -> None:
        with self.assertRaises(Exception):
            blur_photo(b"not an image at all" * 10)


class FakeConn:
    pass


class FakePool:
    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield FakeConn()


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Peer, object]] = []

    async def publish(self, peer: Peer, event: object) -> None:
        self.published.append((peer, event))


class ForwardTests(unittest.TestCase):
    me = Peer("vk", "club1", 1)

    def forward(
        self,
        partner: Peer,
        premium: set[Peer],
        *,
        kind: str = "photo",
        locked_copy: object = "ok",
        tg_file_id: str | None = "FILEID",
    ) -> tuple[str, FakeEvents, list[dict[str, object]]]:
        service = ChatService.__new__(ChatService)
        service._pool = FakePool()  # type: ignore[attr-defined]
        service._events = FakeEvents()  # type: ignore[attr-defined]
        records: list[dict[str, object]] = []

        async def get_session(conn: object, peer: Peer) -> object:
            return SimpleNamespace(phase="chatting", partner=partner, dialog_id=7)

        async def linked(conn: object, peer: Peer, session: object) -> Peer:
            return partner

        async def is_premium(conn: object, peer: Peer) -> bool:
            return peer in premium and not peer.is_engine

        async def insert_message(conn: object, **fields: object) -> None:
            records.append(fields)

        async def good_copy() -> UUID:
            return COPY

        async def bad_copy() -> None:
            return None

        copy = {"ok": good_copy, "fail": bad_copy, "none": None}[locked_copy]  # type: ignore[index]
        with (
            patch.object(chat_module.repo, "get_session", get_session),
            patch.object(chat_module.repo, "user_is_premium", is_premium),
            patch.object(chat_module.messages_repo, "insert_message", insert_message),
            patch.object(ChatService, "_linked_partner", staticmethod(linked)),
        ):
            result = asyncio.run(
                service.forward_media(
                    self.me,
                    ORIGINAL,
                    kind,  # type: ignore[arg-type]
                    "image/jpeg",
                    tg_file_id=tg_file_id,
                    tg_method="photo",
                    locked_copy=copy,  # type: ignore[arg-type]
                )
            )
        return result, service._events, records  # type: ignore[attr-defined]

    def test_partner_without_premium_gets_locked_copy(self) -> None:
        partner = Peer("tg", "bot", 2)
        result, events, records = self.forward(partner, {self.me})
        self.assertEqual(result, "sent")
        (target, event), = events.published
        self.assertEqual(target, partner)
        self.assertEqual(event.media_id, str(COPY))  # type: ignore[attr-defined]
        self.assertTrue(event.locked)  # type: ignore[attr-defined]
        self.assertEqual(event.kind, "photo")  # type: ignore[attr-defined]
        self.assertEqual(event.content_type, "image/jpeg")  # type: ignore[attr-defined]
        self.assertIsNone(event.tg_file_id)  # type: ignore[attr-defined]
        self.assertIsNone(event.tg_method)  # type: ignore[attr-defined]
        self.assertEqual(records[0]["status"], "delivered")
        self.assertEqual(records[0]["media_id"], ORIGINAL)

    def test_premium_partner_gets_original(self) -> None:
        partner = Peer("tg", "bot", 2)
        result, events, _ = self.forward(partner, {partner})
        self.assertEqual(result, "sent")
        event = events.published[0][1]
        self.assertEqual(event.media_id, str(ORIGINAL))  # type: ignore[attr-defined]
        self.assertEqual(event.tg_file_id, "FILEID")  # type: ignore[attr-defined]
        self.assertFalse(event.locked)  # type: ignore[attr-defined]

    def test_engine_partner_gets_original(self) -> None:
        partner = Peer("engine", "acc", 1)
        result, events, _ = self.forward(partner, set())
        self.assertEqual(result, "sent")
        event = events.published[0][1]
        self.assertEqual(event.media_id, str(ORIGINAL))  # type: ignore[attr-defined]
        self.assertFalse(event.locked)  # type: ignore[attr-defined]

    def test_no_bytes_is_skipped_without_event(self) -> None:
        partner = Peer("tg", "bot", 2)
        result, events, records = self.forward(partner, set(), locked_copy="none")
        self.assertEqual(result, "locked")
        self.assertEqual(events.published, [])
        self.assertEqual(records[0]["status"], "skipped")

    def test_failed_copy_is_skipped_without_event(self) -> None:
        partner = Peer("vk", "club2", 2)
        result, events, records = self.forward(partner, set(), locked_copy="fail")
        self.assertEqual(result, "locked")
        self.assertEqual(events.published, [])
        self.assertEqual(records[0]["status"], "skipped")

    def test_video_is_not_blurred(self) -> None:
        partner = Peer("tg", "bot", 2)
        result, events, _ = self.forward(partner, set(), kind="video")
        self.assertEqual(result, "sent")
        self.assertFalse(events.published[0][1].locked)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
