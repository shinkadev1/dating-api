"""SQL раздела «Пользователи» в Bot CRM: только чтение, без переписки."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

# Порядок списка: ключ — значение параметра sort, SQL берётся только отсюда.
SORTS: dict[str, str] = {
    "last_seen": "bu.last_seen_at DESC",
    "started": "bu.started_at DESC",
    "searches": "bu.search_count DESC, bu.last_seen_at DESC",
    "premium": "bu.premium_until DESC NULLS LAST, bu.last_seen_at DESC",
}
# Активность: ключ — значение параметра active.
ACTIVE: dict[str, str] = {
    "24h": "bu.last_seen_at >= %(now)s - INTERVAL '24 hours'",
    "7d": "bu.last_seen_at >= %(now)s - INTERVAL '7 days'",
    "30d": "bu.last_seen_at >= %(now)s - INTERVAL '30 days'",
    "gone": "bu.last_seen_at < %(now)s - INTERVAL '30 days'",
}


def _filters(platforms: list[str], q: str, premium: str, active: str) -> tuple[str, dict[str, Any]]:
    where = ["bu.platform = ANY(%(platforms)s)"]
    args: dict[str, Any] = {"platforms": platforms}
    q = q.strip().lstrip("@")
    if q:
        if q.isdigit():
            where.append("(bu.user_id = %(qid)s OR bu.handle ILIKE %(qlike)s)")
            args["qid"] = int(q)
        else:
            where.append("bu.handle ILIKE %(qlike)s")
        args["qlike"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if premium == "yes":
        where.append("bu.premium_until > %(now)s")
    elif premium == "no":
        where.append("(bu.premium_until IS NULL OR bu.premium_until <= %(now)s)")
    elif premium == "ended":
        where.append("bu.premium_until <= %(now)s")
    if active in ACTIVE:
        where.append(ACTIVE[active])
    return " AND ".join(where), args


async def stats(conn: AsyncConnection, platforms: list[str], now: datetime) -> dict[str, int]:
    cursor = await conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE bu.premium_until > %(now)s) AS premium,
            COUNT(*) FILTER (WHERE bu.last_seen_at >= %(now)s - INTERVAL '24 hours') AS active_24h,
            COUNT(*) FILTER (WHERE bu.last_seen_at >= %(now)s - INTERVAL '7 days') AS active_7d,
            COUNT(*) FILTER (WHERE bu.started_at >= %(now)s - INTERVAL '24 hours') AS new_24h,
            COUNT(*) FILTER (WHERE bu.started_at >= %(now)s - INTERVAL '7 days') AS new_7d,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM chat_sessions s
                WHERE s.platform = bu.platform AND s.user_id = bu.user_id AND s.phase = 'searching'
            )) AS searching,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM chat_sessions s
                WHERE s.platform = bu.platform AND s.user_id = bu.user_id AND s.phase = 'chatting'
            )) AS chatting
        FROM bot_users bu
        WHERE bu.platform = ANY(%(platforms)s)
        """,
        {"platforms": platforms, "now": now},
    )
    row = await cursor.fetchone()
    return {k: int(v or 0) for k, v in (row or {}).items()}


async def search(
    conn: AsyncConnection,
    *,
    platforms: list[str],
    q: str,
    premium: str,
    active: str,
    sort: str,
    limit: int,
    offset: int,
    now: datetime,
) -> tuple[int, list[dict[str, Any]]]:
    where, args = _filters(platforms, q, premium, active)
    args.update(now=now, limit=limit, offset=offset)
    cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM bot_users bu WHERE {where}", args)
    total = int((await cursor.fetchone())["n"])
    cursor = await conn.execute(
        f"""
        SELECT bu.platform, bu.user_id, bu.handle, bu.search_count, bu.started_at, bu.last_seen_at,
               bu.premium_until,
               COALESCE((
                   SELECT s.phase FROM chat_sessions s
                   WHERE s.platform = bu.platform AND s.user_id = bu.user_id AND s.phase <> 'idle'
                   ORDER BY s.updated_at DESC LIMIT 1
               ), 'idle') AS phase,
               (SELECT COUNT(*) FROM user_channels link WHERE link.user_platform = bu.platform
                  AND link.user_id = bu.user_id) AS channels
        FROM bot_users bu
        WHERE {where}
        ORDER BY {SORTS.get(sort, SORTS['last_seen'])}, bu.platform, bu.user_id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        args,
    )
    return total, list(await cursor.fetchall())


async def detail(conn: AsyncConnection, platform: str, user_id: int) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        SELECT bu.platform, bu.user_id, bu.handle, bu.search_count, bu.started_at, bu.last_seen_at,
               bu.premium_until,
               COALESCE((
                   SELECT s.phase FROM chat_sessions s
                   WHERE s.platform = bu.platform AND s.user_id = bu.user_id AND s.phase <> 'idle'
                   ORDER BY s.updated_at DESC LIMIT 1
               ), 'idle') AS phase
        FROM bot_users bu
        WHERE bu.platform = %(platform)s AND bu.user_id = %(user_id)s
        """,
        {"platform": platform, "user_id": user_id},
    )
    user = await cursor.fetchone()
    if not user:
        return None
    args = {"platform": platform, "user_id": user_id}
    cursor = await conn.execute(
        """
        SELECT ch.platform, ch.external_id, ch.title, link.started_at, link.last_seen_at, link.search_count
        FROM user_channels link
        JOIN channels ch ON ch.id = link.channel_id
        WHERE link.user_platform = %(platform)s AND link.user_id = %(user_id)s
        ORDER BY link.last_seen_at DESC
        """,
        args,
    )
    user["chats"] = list(await cursor.fetchall())
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS dialogs, MAX(d.started_at) AS last_dialog_at,
               COUNT(*) FILTER (WHERE d.end_reason = 'reported') AS reported
        FROM dialogs d
        WHERE (d.a_platform = %(platform)s AND d.a_user_id = %(user_id)s)
           OR (d.b_platform = %(platform)s AND d.b_user_id = %(user_id)s)
        """,
        args,
    )
    user.update(await cursor.fetchone())
    cursor = await conn.execute(
        """
        SELECT b.id, b.title, b.text, MAX(log.sent_at) AS sent_at, COUNT(*) AS times
        FROM broadcast_log log
        JOIN broadcasts b ON b.id = log.broadcast_id
        WHERE log.user_platform = %(platform)s AND log.user_id = %(user_id)s
        GROUP BY b.id, b.title, b.text
        ORDER BY MAX(log.sent_at) DESC
        LIMIT 30
        """,
        args,
    )
    user["broadcasts"] = list(await cursor.fetchall())
    return user
