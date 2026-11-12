"""SQL рассылок из CRM: сами рассылки, аудитория и выбор получателей с записью в журнал."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from app.peer import Peer

_COLUMNS = """
    id, title, text, media_id, content_type, platforms, kind, send_at, event, delay_minutes,
    repeat, status, sent_count, created_at, updated_at, active_since, finished_at
"""

# Момент события для пары «человек — чат» (link = user_channels, bu = bot_users).
# Ключи — допустимые значения broadcasts.event; SQL подставляется только из этого словаря.
EVENT_AT: dict[str, str] = {
    "first_seen": "link.started_at",
    "last_seen": "link.last_seen_at",
    "dialog_ended": """(
        SELECT MAX(d.ended_at) FROM dialogs d
        WHERE (d.a_platform = link.user_platform AND d.a_user_id = link.user_id)
           OR (d.b_platform = link.user_platform AND d.b_user_id = link.user_id)
    )""",
    "premium_ended": "CASE WHEN bu.premium_until <= %(now)s THEN bu.premium_until END",
}


async def list_all(conn: AsyncConnection) -> list[dict[str, Any]]:
    cursor = await conn.execute(f"SELECT {_COLUMNS} FROM broadcasts ORDER BY created_at DESC, id DESC")
    return list(await cursor.fetchall())


async def get(conn: AsyncConnection, broadcast_id: int) -> dict[str, Any] | None:
    cursor = await conn.execute(f"SELECT {_COLUMNS} FROM broadcasts WHERE id = %s", (broadcast_id,))
    return await cursor.fetchone()


async def create(conn: AsyncConnection, values: dict[str, Any]) -> dict[str, Any]:
    cursor = await conn.execute(
        f"""
        INSERT INTO broadcasts (title, text, platforms, kind, send_at, event, delay_minutes, repeat, status, active_since)
        VALUES (%(title)s, %(text)s, %(platforms)s, %(kind)s, %(send_at)s, %(event)s, %(delay_minutes)s,
                %(repeat)s, %(status)s, %(active_since)s)
        RETURNING {_COLUMNS}
        """,
        values,
    )
    return await cursor.fetchone()


async def update(conn: AsyncConnection, broadcast_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
    """Меняет переданные поля. Имена колонок — только из вызывающего кода, не из запроса."""
    if not values:
        return await get(conn, broadcast_id)
    sets = ", ".join(f"{name} = %({name})s" for name in values)
    cursor = await conn.execute(
        f"UPDATE broadcasts SET {sets}, updated_at = NOW() WHERE id = %(id)s RETURNING {_COLUMNS}",
        {**values, "id": broadcast_id},
    )
    return await cursor.fetchone()


async def delete(conn: AsyncConnection, broadcast_id: int) -> None:
    await conn.execute("DELETE FROM broadcasts WHERE id = %s", (broadcast_id,))


async def audience(conn: AsyncConnection, platforms: list[str]) -> int:
    """Сколько пар «человек — чат» получат рассылку по времени."""
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM user_channels link
        JOIN channels ch ON ch.id = link.channel_id
        WHERE ch.platform = ANY(%(platforms)s) AND ch.enabled AND link.user_platform = ch.platform
        """,
        {"platforms": platforms},
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def due_scheduled(conn: AsyncConnection, now: datetime) -> list[dict[str, Any]]:
    """Рассылки по времени, которым пора: запланированные и прерванные посреди отправки."""
    cursor = await conn.execute(
        f"""
        SELECT {_COLUMNS} FROM broadcasts
        WHERE kind = 'at' AND (status = 'sending' OR (status = 'scheduled' AND send_at <= %s))
        ORDER BY send_at, id
        """,
        (now,),
    )
    return list(await cursor.fetchall())


async def active_events(conn: AsyncConnection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM broadcasts WHERE kind = 'event' AND status = 'active' ORDER BY id"
    )
    return list(await cursor.fetchall())


async def set_status(conn: AsyncConnection, broadcast_id: int, status: str, *, finished: bool = False) -> None:
    await conn.execute(
        """
        UPDATE broadcasts
        SET status = %s, updated_at = NOW(), finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END
        WHERE id = %s
        """,
        (status, finished, broadcast_id),
    )


async def add_sent(conn: AsyncConnection, broadcast_id: int, count: int) -> None:
    await conn.execute(
        "UPDATE broadcasts SET sent_count = sent_count + %s, updated_at = NOW() WHERE id = %s",
        (count, broadcast_id),
    )


async def claim_scheduled(conn: AsyncConnection, broadcast: dict[str, Any], *, limit: int) -> list[Peer]:
    """Следующая пачка получателей рассылки по времени; сразу пишет их в журнал.

    Фиксировать транзакцию должен вызывающий: журнал и постановка в очередь идут вместе.
    """
    cursor = await conn.execute(
        """
        WITH candidates AS (
            SELECT link.user_platform, link.user_id, link.channel_id
            FROM user_channels link
            JOIN channels ch ON ch.id = link.channel_id
            WHERE ch.platform = ANY(%(platforms)s)
              AND ch.enabled
              AND link.user_platform = ch.platform
              AND NOT EXISTS (
                  SELECT 1 FROM broadcast_log log
                  WHERE log.broadcast_id = %(id)s
                    AND log.user_platform = link.user_platform
                    AND log.user_id = link.user_id
                    AND log.channel_id = link.channel_id
              )
            ORDER BY link.user_platform, link.user_id, link.channel_id
            LIMIT %(limit)s
        ),
        logged AS (
            INSERT INTO broadcast_log (broadcast_id, user_platform, user_id, channel_id)
            SELECT %(id)s, user_platform, user_id, channel_id FROM candidates
            ON CONFLICT DO NOTHING
            RETURNING user_platform, user_id, channel_id
        )
        SELECT ch.platform, ch.external_id, logged.user_id
        FROM logged
        JOIN channels ch ON ch.id = logged.channel_id
        """,
        {"platforms": list(broadcast["platforms"]), "id": broadcast["id"], "limit": limit},
    )
    return _peers(await cursor.fetchall())


async def claim_event(
    conn: AsyncConnection,
    broadcast: dict[str, Any],
    *,
    now: datetime,
    limit: int,
) -> list[Peer]:
    """Пачка получателей рассылки после события: событие было не раньше active_since,
    с него прошло delay_minutes, человек сейчас не в поиске и не в диалоге.

    repeat=false — человеку в этом чате один раз; repeat=true — после каждого нового события.
    """
    event_sql = EVENT_AT[broadcast["event"]]
    already = (
        "AND log.event_at >= cand.event_at" if broadcast["repeat"] else ""
    )
    cursor = await conn.execute(
        f"""
        WITH events AS (
            SELECT link.user_platform, link.user_id, link.channel_id, {event_sql} AS event_at
            FROM user_channels link
            JOIN channels ch ON ch.id = link.channel_id
            LEFT JOIN bot_users bu ON bu.platform = link.user_platform AND bu.user_id = link.user_id
            WHERE ch.platform = ANY(%(platforms)s) AND ch.enabled AND link.user_platform = ch.platform
        ),
        candidates AS (
            SELECT cand.user_platform, cand.user_id, cand.channel_id, cand.event_at
            FROM events cand
            WHERE cand.event_at IS NOT NULL
              AND cand.event_at >= %(active_since)s
              AND cand.event_at <= %(now)s - make_interval(mins => %(delay)s)
              AND NOT EXISTS (
                  SELECT 1 FROM broadcast_log log
                  WHERE log.broadcast_id = %(id)s
                    AND log.user_platform = cand.user_platform
                    AND log.user_id = cand.user_id
                    AND log.channel_id = cand.channel_id
                    {already}
              )
              AND NOT EXISTS (
                  SELECT 1 FROM chat_sessions s
                  WHERE s.platform = cand.user_platform
                    AND s.user_id = cand.user_id
                    AND s.phase IN ('searching', 'chatting')
              )
            ORDER BY cand.event_at, cand.user_platform, cand.user_id, cand.channel_id
            LIMIT %(limit)s
        ),
        logged AS (
            INSERT INTO broadcast_log (broadcast_id, user_platform, user_id, channel_id, event_at)
            SELECT %(id)s, user_platform, user_id, channel_id, event_at FROM candidates
            ON CONFLICT DO NOTHING
            RETURNING user_platform, user_id, channel_id
        )
        SELECT ch.platform, ch.external_id, logged.user_id
        FROM logged
        JOIN channels ch ON ch.id = logged.channel_id
        """,
        {
            "platforms": list(broadcast["platforms"]),
            "id": broadcast["id"],
            "active_since": broadcast["active_since"],
            "delay": int(broadcast["delay_minutes"]),
            "now": now,
            "limit": limit,
        },
    )
    return _peers(await cursor.fetchall())


def _peers(rows: list[dict[str, Any]]) -> list[Peer]:
    return [Peer(str(r["platform"]), str(r["external_id"]), int(r["user_id"])) for r in rows]
