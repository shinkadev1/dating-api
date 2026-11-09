"""Запросы к репликам диалогов и журналу выгрузок в S3."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from app.peer import Peer

_ARCHIVE_LOCK = 7_310_003


async def insert_message(
    conn: AsyncConnection,
    *,
    dialog_id: int,
    sender: Peer,
    partner: Peer | None,
    kind: str,
    status: str,
    text: str | None = None,
    media_id: UUID | None = None,
    tg_file_id: str | None = None,
    tg_method: str | None = None,
    content_type: str | None = None,
) -> bool:
    """Пишет реплику, если sender (и partner, когда он известен) — стороны этого диалога.

    Сторону 'a' или 'b' база определяет сама по диалогу. False — строка не записана.
    """
    cursor = await conn.execute(
        """
        INSERT INTO messages (dialog_id, sender, kind, status, text, media_id, tg_file_id, tg_method, content_type)
        SELECT d.id,
               CASE WHEN d.a_platform = %(s_platform)s AND d.a_bot_key = %(s_bot_key)s
                         AND d.a_user_id = %(s_user_id)s THEN 'a' ELSE 'b' END,
               %(kind)s::text, %(status)s::text, %(text)s::text, %(media_id)s::uuid,
               %(tg_file_id)s::text, %(tg_method)s::text, %(content_type)s::text
        FROM dialogs AS d
        WHERE d.id = %(dialog_id)s
          AND (
            (d.a_platform = %(s_platform)s AND d.a_bot_key = %(s_bot_key)s AND d.a_user_id = %(s_user_id)s
             AND (%(p_platform)s::text IS NULL
                  OR (d.b_platform = %(p_platform)s::text AND d.b_bot_key = %(p_bot_key)s::text
                      AND d.b_user_id = %(p_user_id)s::bigint)))
            OR
            (d.b_platform = %(s_platform)s AND d.b_bot_key = %(s_bot_key)s AND d.b_user_id = %(s_user_id)s
             AND (%(p_platform)s::text IS NULL
                  OR (d.a_platform = %(p_platform)s::text AND d.a_bot_key = %(p_bot_key)s::text
                      AND d.a_user_id = %(p_user_id)s::bigint)))
          )
        """,
        {
            "dialog_id": dialog_id,
            "s_platform": sender.platform,
            "s_bot_key": sender.bot_key,
            "s_user_id": sender.user_id,
            "p_platform": partner.platform if partner else None,
            "p_bot_key": partner.bot_key if partner else None,
            "p_user_id": partner.user_id if partner else None,
            "kind": kind,
            "status": status,
            "text": text,
            "media_id": media_id,
            "tg_file_id": tg_file_id,
            "tg_method": tg_method,
            "content_type": content_type,
        },
    )
    return cursor.rowcount == 1


async def try_archive_lock(conn: AsyncConnection) -> bool:
    """Сессионная блокировка выгрузки: второй проход (другая копия API) не стартует."""
    cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (_ARCHIVE_LOCK,))
    row = await cursor.fetchone()
    return bool(row["locked"])


async def archive_unlock(conn: AsyncConnection) -> None:
    await conn.execute("SELECT pg_advisory_unlock(%s)", (_ARCHIVE_LOCK,))


async def oldest_message_at(conn: AsyncConnection) -> datetime | None:
    cursor = await conn.execute("SELECT min(created_at) AS oldest FROM messages")
    row = await cursor.fetchone()
    return row["oldest"]


async def day_rows(conn: AsyncConnection, start: datetime, end: datetime, limit: int) -> list[dict[str, Any]]:
    """Реплики за [start, end) вместе с данными диалога, по возрастанию id."""
    cursor = await conn.execute(
        """
        SELECT m.id, m.dialog_id, m.sender, m.created_at, m.kind, m.text, m.media_id,
               m.tg_file_id, m.tg_method, m.content_type, m.status,
               d.a_platform, d.a_bot_key, d.a_user_id, d.b_platform, d.b_bot_key, d.b_user_id,
               d.started_at, d.ended_at, d.end_reason, d.ended_by
        FROM messages AS m
        JOIN dialogs AS d ON d.id = m.dialog_id
        WHERE m.created_at >= %(start)s AND m.created_at < %(end)s
        ORDER BY m.id
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit},
    )
    return list(await cursor.fetchall())


async def next_part(conn: AsyncConnection, day: date) -> int:
    cursor = await conn.execute(
        "SELECT COALESCE(max(part), 0) + 1 AS part FROM message_archives WHERE day = %s",
        (day,),
    )
    row = await cursor.fetchone()
    return int(row["part"])


async def commit_archive(
    conn: AsyncConnection,
    *,
    day: date,
    part: int,
    key: str,
    rows_count: int,
    size_bytes: int,
    ids: list[int],
) -> None:
    """Записывает выгрузку в журнал и удаляет ровно эти реплики одной транзакцией.

    Если удалено не столько строк, сколько выгружено, или запись в журнале не прошла, откат.
    """
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO message_archives (day, part, object_key, rows_count, size_bytes,
                                          first_message_id, last_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (day, part, key, rows_count, size_bytes, min(ids), max(ids)),
        )
        cursor = await conn.execute("DELETE FROM messages WHERE id = ANY(%s)", (ids,))
        if cursor.rowcount != len(ids):
            raise RuntimeError(f"Удалено {cursor.rowcount} реплик вместо {len(ids)}")
