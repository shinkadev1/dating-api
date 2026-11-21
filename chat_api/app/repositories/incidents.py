"""Журнал инцидентов поиска и сведения об аккаунтах chat-engine для определения причины."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from app.peer import ENGINE_PLATFORM, Peer


async def engine_accounts(conn: AsyncConnection, presence_window_seconds: float) -> list[dict[str, Any]]:
    """Аккаунты chat-engine, подавшие признак жизни за окно, с их фазой в Chat API."""
    cursor = await conn.execute(
        """
        SELECT p.bot_key, p.mode, p.last_error, p.last_error_at,
               p.last_hunt_normal_at, p.last_hunt_adult_at,
               COALESCE(s.phase, 'idle') AS state
        FROM engine_presence AS p
        LEFT JOIN chat_sessions AS s
               ON s.platform = %(engine)s AND s.bot_key = p.bot_key AND s.user_id = 1
        WHERE p.last_seen_at >= NOW() - make_interval(secs => %(window)s)
        ORDER BY p.bot_key
        """,
        {"engine": ENGINE_PLATFORM, "window": presence_window_seconds},
    )
    return list(await cursor.fetchall())


async def queue_by_mode(conn: AsyncConnection) -> dict[str, dict[str, int]]:
    """Сколько людей ждут и сколько аккаунтов chat-engine ищут, по режимам."""
    cursor = await conn.execute(
        """
        SELECT mode,
               count(*) FILTER (WHERE platform <> %(engine)s) AS waiting,
               count(*) FILTER (WHERE platform = %(engine)s) AS hunting
        FROM chat_sessions
        WHERE phase = 'searching'
        GROUP BY mode
        """,
        {"engine": ENGINE_PLATFORM},
    )
    result = {"waiting": {"normal": 0, "adult": 0}, "hunting": {"normal": 0, "adult": 0}}
    for row in await cursor.fetchall():
        result["waiting"][row["mode"]] = int(row["waiting"])
        result["hunting"][row["mode"]] = int(row["hunting"])
    return result


async def insert_incident(
    conn: AsyncConnection,
    peer: Peer,
    *,
    mode: str,
    searching_since: datetime | None,
    ended_at: datetime,
    wait_seconds: int,
    outcome: str,
    reason: str,
    snapshot: dict[str, Any] | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO search_incidents
            (platform, bot_key, user_id, mode, searching_since, ended_at, wait_seconds, outcome, reason, snapshot)
        VALUES (%(platform)s, %(bot_key)s, %(user_id)s, %(mode)s, %(since)s, %(ended_at)s,
                %(wait)s, %(outcome)s, %(reason)s, %(snapshot)s::jsonb)
        """,
        {
            **peer.as_dict(),
            "mode": mode,
            "since": searching_since,
            "ended_at": ended_at,
            "wait": wait_seconds,
            "outcome": outcome,
            "reason": reason,
            "snapshot": None if snapshot is None else json.dumps(snapshot, ensure_ascii=False),
        },
    )


async def set_engine_status(
    conn: AsyncConnection,
    bot_key: str,
    *,
    mode: str | None,
    error: str | None,
    error_given: bool,
) -> None:
    """Режим и последняя ошибка аккаунта от моста. Без error_given ошибка не меняется; error=None очищает."""
    await conn.execute(
        """
        INSERT INTO engine_presence (bot_key, mode, last_error, last_error_at)
        VALUES (%(bot_key)s, %(mode)s::text, %(error)s::text,
                CASE WHEN %(error)s::text IS NOT NULL THEN NOW() END)
        ON CONFLICT (bot_key) DO UPDATE SET
            last_seen_at = NOW(),
            mode = COALESCE(EXCLUDED.mode, engine_presence.mode),
            last_error = CASE WHEN %(given)s::boolean THEN EXCLUDED.last_error ELSE engine_presence.last_error END,
            last_error_at = CASE WHEN %(given)s::boolean THEN EXCLUDED.last_error_at
                                 ELSE engine_presence.last_error_at END
        """,
        {"bot_key": bot_key, "mode": mode, "error": error, "given": error_given},
    )


async def mark_engine_hunt(conn: AsyncConnection, bot_key: str, mode: str) -> None:
    """Аккаунт начал поиск в режиме mode: нужно, чтобы отличить «мост искал» от «мост не искал»."""
    column = "last_hunt_adult_at" if mode == "adult" else "last_hunt_normal_at"
    await conn.execute(
        f"""
        INSERT INTO engine_presence (bot_key, {column})
        VALUES (%(bot_key)s, NOW())
        ON CONFLICT (bot_key) DO UPDATE SET last_seen_at = NOW(), {column} = NOW()
        """,
        {"bot_key": bot_key},
    )


def _incident_filters(
    date_from: datetime | None,
    date_to: datetime | None,
    mode: str | None,
    reason: str | None,
    platform: str | None,
) -> tuple[str, dict[str, Any]]:
    where = ["TRUE"]
    params: dict[str, Any] = {}
    if date_from is not None:
        where.append("created_at >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("created_at < %(date_to)s")
        params["date_to"] = date_to
    if mode:
        where.append("mode = %(mode)s")
        params["mode"] = mode
    if reason:
        where.append("reason = %(reason)s")
        params["reason"] = reason
    if platform:
        where.append("platform = %(platform)s")
        params["platform"] = platform
    return " AND ".join(where), params


async def list_incidents(
    conn: AsyncConnection,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    mode: str | None,
    reason: str | None,
    platform: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Страница инцидентов (новые первыми) и сводка count по reason за тот же период и фильтры."""
    where, params = _incident_filters(date_from, date_to, mode, reason, platform)
    cursor = await conn.execute(
        f"""
        SELECT id, created_at, platform, bot_key, user_id, mode, searching_since, ended_at,
               wait_seconds, outcome, reason, snapshot
        FROM search_incidents
        WHERE {where}
        ORDER BY created_at DESC, id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": limit, "offset": offset},
    )
    items = list(await cursor.fetchall())
    cursor = await conn.execute(
        f"SELECT reason, count(*) AS n FROM search_incidents WHERE {where} GROUP BY reason",
        params,
    )
    summary = {row["reason"]: int(row["n"]) for row in await cursor.fetchall()}
    return items, summary
