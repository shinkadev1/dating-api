"""Кука сайта и пользователь web, которого она обозначает."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

# Тот же диапазон, что у гостевого JWT на сайте: больше id воркер не примет.
_MAX_USER_ID = 2**48 - 1
# Регистрация куки — ещё не сообщение и не поиск, в онлайн её не считаем.
_NOT_ONLINE = "1970-01-01T00:00:00Z"
_ATTEMPTS = 5
_COOKIE_CONSTRAINT = "web_visitors_pkey"
_USER_CONSTRAINTS = frozenset({"web_visitors_user_id_key", "web_visitors_platform_user_id_key"})


@dataclass(frozen=True)
class WebVisitor:
    id: UUID
    user_id: int
    created: bool


class _UserIdTaken(Exception):
    """Случайный user_id уже есть. Нужна новая попытка."""


async def save_web_visitor(
    conn: AsyncConnection,
    visitor_id: UUID | None,
    bot_key: str,
    user_id: int | None = None,
) -> WebVisitor:
    """Возвращает куку и пользователя.

    Известная кука остаётся при своём человеке. Новая кука с user_id привязывается
    к уже существующему гостю чата. Без обоих создаётся новый пользователь сайта.
    """
    if visitor_id is not None:
        existing = await _touch(conn, visitor_id)
        if existing is not None:
            return existing

    cookie_id = visitor_id or uuid.uuid4()
    if user_id is not None:
        return await _bind(conn, cookie_id, user_id, bot_key)

    for _ in range(_ATTEMPTS):
        candidate = secrets.randbelow(_MAX_USER_ID) + 1
        try:
            await _insert_pair(conn, cookie_id, candidate, bot_key, allow_existing=False)
        except _UserIdTaken:
            continue
        except UniqueViolation as exc:
            resolved = await _resolve_conflict(conn, exc, cookie_id, candidate)
            if resolved is not None:
                return resolved
            raise
        return WebVisitor(cookie_id, candidate, True)
    raise RuntimeError("Не удалось сохранить посетителя сайта")


async def _bind(conn: AsyncConnection, cookie_id: UUID, user_id: int, bot_key: str) -> WebVisitor:
    owned = await _cookie_of_user(conn, user_id)
    if owned is not None:
        return await _touch(conn, owned) or WebVisitor(owned, user_id, False)
    try:
        await _insert_pair(conn, cookie_id, user_id, bot_key, allow_existing=True)
    except UniqueViolation as exc:
        resolved = await _resolve_conflict(conn, exc, cookie_id, user_id)
        if resolved is not None:
            return resolved
        raise
    return WebVisitor(cookie_id, user_id, True)


async def _resolve_conflict(
    conn: AsyncConnection,
    exc: UniqueViolation,
    cookie_id: UUID,
    user_id: int,
) -> WebVisitor | None:
    name = getattr(exc.diag, "constraint_name", None)
    if name == _COOKIE_CONSTRAINT:
        return await _touch(conn, cookie_id)
    if name in _USER_CONSTRAINTS:
        owned = await _cookie_of_user(conn, user_id)
        if owned is None:
            return None
        return await _touch(conn, owned)
    return None


async def _touch(conn: AsyncConnection, visitor_id: UUID) -> WebVisitor | None:
    cursor = await conn.execute(
        """
        UPDATE web_visitors
        SET visits = visits + 1,
            last_seen_at = NOW()
        WHERE id = %(id)s
        RETURNING id, user_id
        """,
        {"id": visitor_id},
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return WebVisitor(row["id"], int(row["user_id"]), False)


async def _cookie_of_user(conn: AsyncConnection, user_id: int) -> UUID | None:
    cursor = await conn.execute(
        "SELECT id FROM web_visitors WHERE platform = 'web' AND user_id = %(user_id)s",
        {"user_id": user_id},
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return row["id"]


async def _insert_pair(
    conn: AsyncConnection,
    visitor_id: UUID,
    user_id: int,
    bot_key: str,
    *,
    allow_existing: bool,
) -> None:
    async with conn.transaction():
        inserted = await _insert_user(conn, user_id)
        if not inserted and not allow_existing:
            raise _UserIdTaken
        await _link_channel(conn, user_id, bot_key)
        await conn.execute(
            """
            INSERT INTO web_visitors (id, platform, user_id)
            VALUES (%(id)s, 'web', %(user_id)s)
            """,
            {"id": visitor_id, "user_id": user_id},
        )


async def _insert_user(conn: AsyncConnection, user_id: int) -> bool:
    cursor = await conn.execute(
        """
        INSERT INTO bot_users (platform, user_id, search_count, last_seen_at)
        VALUES ('web', %(user_id)s, 0, %(seen)s)
        ON CONFLICT (platform, user_id) DO NOTHING
        RETURNING user_id
        """,
        {"user_id": user_id, "seen": _NOT_ONLINE},
    )
    return await cursor.fetchone() is not None


async def _link_channel(conn: AsyncConnection, user_id: int, bot_key: str) -> None:
    await conn.execute(
        """
        WITH channel AS (
            INSERT INTO channels (platform, external_id)
            VALUES ('web', %(bot_key)s)
            ON CONFLICT (platform, external_id) DO UPDATE SET title = channels.title
            RETURNING id
        )
        INSERT INTO user_channels (user_platform, user_id, channel_id, search_count, last_seen_at)
        SELECT 'web', %(user_id)s, channel.id, 0, %(seen)s
        FROM channel
        ON CONFLICT (user_platform, user_id, channel_id) DO NOTHING
        """,
        {"user_id": user_id, "bot_key": bot_key, "seen": _NOT_ONLINE},
    )
