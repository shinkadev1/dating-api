"""Приём файла от бота: проверка байтов, запись в S3 и строка в stored_media."""

from __future__ import annotations

import logging
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from app.media_types import KINDS, MAX_BYTES, MediaRejected, detect_content_type, object_key
from app.peer import Peer
from app.repositories.media import insert_media
from app.schemas import MediaOut
from app.services.storage import ObjectStorage, StorageError

logger = logging.getLogger(__name__)


class StorageDisabled(Exception):
    """Ключи S3 не заданы или бакет не открылся при старте."""


class MediaService:
    def __init__(self, pool: AsyncConnectionPool, storage: ObjectStorage) -> None:
        self._pool = pool
        self._storage = storage

    async def save(self, peer: Peer, kind: str, body: bytes) -> MediaOut:
        if not self._storage.enabled:
            raise StorageDisabled
        if kind not in KINDS:
            raise MediaRejected(400, "Неизвестный вид медиа")
        if not body:
            raise MediaRejected(400, "Пустой файл")
        limit = MAX_BYTES[kind]
        if len(body) > limit:
            raise MediaRejected(413, "Файл слишком большой")
        content_type = detect_content_type(kind, body)
        if content_type is None:
            raise MediaRejected(415, "Файл не похож на фото, видео, кружок или голосовое")

        media_id = uuid4()
        key = object_key(peer, media_id, content_type)
        await self._storage.put(key, body, content_type)
        try:
            async with self._pool.connection() as conn:
                await insert_media(
                    conn,
                    media_id=media_id,
                    peer=peer,
                    kind=kind,
                    object_key=key,
                    content_type=content_type,
                    size_bytes=len(body),
                )
        except Exception:
            logger.exception("Не записал медиа %s в базу", key)
            await self._storage.delete(key)
            raise StorageError(key)
        logger.info(
            "Медиа %s %s/%s/%s -> %s (%s байт)",
            kind,
            peer.platform,
            peer.bot_key,
            peer.user_id,
            key,
            len(body),
        )
        return MediaOut(id=media_id, kind=kind, key=key, content_type=content_type, size=len(body))
