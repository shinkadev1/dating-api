"""Записи о файлах, которые уже лежат в S3."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from app.peer import Peer


async def insert_media(
    conn: AsyncConnection,
    *,
    media_id: UUID,
    peer: Peer,
    kind: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO stored_media (
            id, platform, bot_key, user_id, kind, object_key, content_type, size_bytes
        )
        VALUES (
            %(id)s, %(platform)s, %(bot_key)s, %(user_id)s, %(kind)s,
            %(object_key)s, %(content_type)s, %(size_bytes)s
        )
        """,
        {
            "id": media_id,
            "kind": kind,
            "object_key": object_key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            **peer.as_dict(),
        },
    )


async def get_media(conn: AsyncConnection, media_id: UUID) -> tuple[str, str, str] | None:
    """Вид, ключ объекта и MIME. None, если записи нет."""
    cursor = await conn.execute(
        """
        SELECT kind, object_key, content_type
        FROM stored_media
        WHERE id = %(id)s
        """,
        {"id": media_id},
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["kind"]), str(row["object_key"]), str(row["content_type"])
