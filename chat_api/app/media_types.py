"""Тип файла по содержимому и пределы размера. Имя и Content-Type клиента не используются."""

from __future__ import annotations

import re
from uuid import UUID

from app.peer import Peer

KINDS = ("photo", "video", "video_note", "voice")

# Совпадает с пределом, который бот проверяет до отправки.
MAX_BYTES: dict[str, int] = {
    "photo": 20 * 1024 * 1024,
    "voice": 20 * 1024 * 1024,
    "video_note": 32 * 1024 * 1024,
    "video": 100 * 1024 * 1024,
}

_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}

_KIND_TYPES = {
    "photo": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/quicktime", "video/webm"},
    "video_note": {"video/mp4", "video/webm"},
    "voice": {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"},
}

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class MediaRejected(Exception):
    """Файл нельзя класть в бакет: пустой, слишком большой или чужого типа."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _mp3_frame(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _ftyp_type(data: bytes, kind: str) -> str | None:
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    brand = data[8:12]
    # Голосовое и аудио Telegram часто лежат в mp4 с брендом isom или mp42, не M4A.
    if kind == "voice" or brand in {b"M4A ", b"M4B ", b"M4P "}:
        return "audio/mp4"
    if brand == b"qt  ":
        return "video/quicktime"
    return "video/mp4"


def detect_content_type(kind: str, data: bytes) -> str | None:
    """Возвращает MIME, если байты подходят виду медиа. Иначе None."""
    if data.startswith(b"\xff\xd8\xff"):
        found = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        found = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        found = "image/gif"
    elif data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        found = "image/webp"
    elif data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE":
        found = "audio/wav"
    elif data.startswith(b"OggS"):
        found = "audio/ogg"
    elif data.startswith(b"ID3") or (kind == "voice" and _mp3_frame(data)):
        found = "audio/mpeg"
    elif data.startswith(b"\x1a\x45\xdf\xa3"):
        found = "audio/webm" if kind == "voice" else "video/webm"
    else:
        found = _ftyp_type(data, kind)
    if found is None or found not in _KIND_TYPES.get(kind, ()):
        return None
    return found


def object_key(peer: Peer, media_id: UUID, content_type: str) -> str:
    """Ключ не берёт имя файла пользователя, чтобы в бакет нельзя было записать чужой путь."""
    bot_key = _SAFE.sub("_", peer.bot_key).strip("._") or "bot"
    ext = _EXT[content_type]
    return f"media/{peer.platform}/{bot_key}/{peer.user_id}/{media_id}{ext}"
