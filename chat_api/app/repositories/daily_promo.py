"""SQL ежедневной рассылки: день с моментом отправки и журнал получателей."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg import AsyncConnection

from app.peer import Peer


async def ensure_day(conn: AsyncConnection, day: date, send_at: datetime) -> dict[str, Any]:
    """Создаёт строку дня, если её нет; возвращает действующую (send_at не меняется)."""
    await conn.execute(
        "INSERT INTO daily_promos (day, send_at) VALUES (%s, %s) ON CONFLICT (day) DO NOTHING",
        (day, send_at),
    )
    cursor = await conn.execute("SELECT day, send_at, sent_count FROM daily_promos WHERE day = %s", (day,))
    return await cursor.fetchone()


async def claim(
    conn: AsyncConnection,
    day: date,
    send_at: datetime,
    platforms: tuple[str, ...],
    *,
    limit: int,
) -> list[Peer]:
    """Следующая пачка получателей; сразу пишет их в журнал дня.

    Один человек - один раз за день, через включённый чат с последней активностью.
    Не берёт тех, кто сейчас в поиске или диалоге, и связки, созданные после send_at.
    Фиксировать транзакцию должен вызывающий: журнал и постановка в очередь идут вместе.
    """
    cursor = await conn.execute(
        """
        WITH candidates AS (
            SELECT DISTINCT ON (link.user_platform, link.user_id)
                   link.user_platform, link.user_id, link.channel_id
            FROM user_channels link
            JOIN channels ch ON ch.id = link.channel_id
            WHERE ch.platform = ANY(%(platforms)s)
              AND ch.enabled
              AND link.user_platform = ch.platform
              AND link.started_at <= %(send_at)s
              AND NOT EXISTS (
                  SELECT 1 FROM daily_promo_log log
                  WHERE log.day = %(day)s
                    AND log.user_platform = link.user_platform
                    AND log.user_id = link.user_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM chat_sessions s
                  WHERE s.platform = link.user_platform
                    AND s.user_id = link.user_id
                    AND s.phase IN ('searching', 'chatting')
              )
            ORDER BY link.user_platform, link.user_id, link.last_seen_at DESC NULLS LAST, link.channel_id
        ),
        picked AS (
            SELECT * FROM candidates
            ORDER BY user_platform, user_id
            LIMIT %(limit)s
        ),
        logged AS (
            INSERT INTO daily_promo_log (day, user_platform, user_id, channel_id)
            SELECT %(day)s, user_platform, user_id, channel_id FROM picked
            ON CONFLICT DO NOTHING
            RETURNING user_platform, user_id, channel_id
        )
        SELECT ch.platform, ch.external_id, logged.user_id
        FROM logged
        JOIN channels ch ON ch.id = logged.channel_id
        """,
        {"platforms": list(platforms), "day": day, "send_at": send_at, "limit": limit},
    )
    rows = await cursor.fetchall()
    return [Peer(str(r["platform"]), str(r["external_id"]), int(r["user_id"])) for r in rows]


async def add_sent(conn: AsyncConnection, day: date, count: int) -> None:
    await conn.execute("UPDATE daily_promos SET sent_count = sent_count + %s WHERE day = %s", (count, day))
