"""SQL напоминаний: выбор получателей и запись в журнал одним запросом."""

from __future__ import annotations

from datetime import datetime

from psycopg import AsyncConnection

from app.peer import Peer


async def table_exists(conn: AsyncConnection) -> bool:
    cursor = await conn.execute("SELECT to_regclass('reminder_log') IS NOT NULL AS ok")
    row = await cursor.fetchone()
    return bool(row and row["ok"])


async def claim_batch(
    conn: AsyncConnection,
    *,
    platforms: list[str],
    seen_before: datetime,
    seen_after: datetime,
    logged_after: datetime,
    limit: int,
) -> list[Peer]:
    """Выбирает получателей и сразу пишет их в журнал. Фиксировать должен вызывающий."""
    cursor = await conn.execute(
        """
        WITH candidates AS (
            SELECT link.user_platform, link.user_id, link.channel_id
            FROM user_channels link
            JOIN channels ch ON ch.id = link.channel_id
            WHERE ch.platform = ANY(%(platforms)s)
              AND link.user_platform = ch.platform
              AND link.last_seen_at <= %(seen_before)s
              AND link.last_seen_at >= %(seen_after)s
              AND NOT EXISTS (
                  SELECT 1 FROM reminder_log log
                  WHERE log.user_platform = link.user_platform
                    AND log.user_id = link.user_id
                    AND log.channel_id = link.channel_id
                    AND log.sent_at > %(logged_after)s
              )
              AND NOT EXISTS (
                  SELECT 1 FROM chat_sessions s
                  WHERE s.platform = link.user_platform
                    AND s.user_id = link.user_id
                    AND s.phase IN ('searching', 'chatting')
              )
            ORDER BY link.last_seen_at DESC, link.user_platform, link.user_id, link.channel_id
            LIMIT %(limit)s
        ),
        logged AS (
            INSERT INTO reminder_log (user_platform, user_id, channel_id)
            SELECT user_platform, user_id, channel_id FROM candidates
            RETURNING user_platform, user_id, channel_id
        )
        SELECT ch.platform, ch.external_id, logged.user_id
        FROM logged
        JOIN channels ch ON ch.id = logged.channel_id
        """,
        {
            "platforms": platforms,
            "seen_before": seen_before,
            "seen_after": seen_after,
            "logged_after": logged_after,
            "limit": limit,
        },
    )
    rows = await cursor.fetchall()
    return [Peer(str(r["platform"]), str(r["external_id"]), int(r["user_id"])) for r in rows]
