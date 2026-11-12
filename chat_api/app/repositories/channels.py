"""SQL сообществ ВК, управляемых из CRM: ключи (зашифрованные), переключатель и статус."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

# external_id сообщества ВК — положительное число; всё остальное в таблице игнорируется.
_VK = "platform = 'vk' AND external_id ~ '^[0-9]+$'"

_COLUMNS = """
    external_id::bigint AS group_id, title, photo_url, token_hint, (token_encrypted IS NOT NULL) AS has_token,
    enabled, status, status_error, last_seen_at
"""


async def list_vk(conn: AsyncConnection) -> list[dict[str, Any]]:
    cursor = await conn.execute(f"SELECT {_COLUMNS} FROM channels WHERE {_VK} ORDER BY title, external_id::bigint")
    return list(await cursor.fetchall())


async def get_vk(conn: AsyncConnection, group_id: int) -> dict[str, Any] | None:
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM channels WHERE {_VK} AND external_id = %s", (str(group_id),)
    )
    return await cursor.fetchone()


async def connect_vk(
    conn: AsyncConnection, group_id: int, *, title: str, photo_url: str, token_encrypted: str, token_hint: str
) -> dict[str, Any]:
    """Создаёт сообщество или обновляет существующее: новый ключ, включено, статус неизвестен."""
    cursor = await conn.execute(
        f"""
        INSERT INTO channels (platform, external_id, title, photo_url, token_encrypted, token_hint, token_updated_at)
        VALUES ('vk', %(id)s, %(title)s, %(photo)s, %(token)s, %(hint)s, NOW())
        ON CONFLICT (platform, external_id) DO UPDATE SET
            title = CASE WHEN EXCLUDED.title <> '' THEN EXCLUDED.title ELSE channels.title END,
            photo_url = EXCLUDED.photo_url,
            token_encrypted = EXCLUDED.token_encrypted,
            token_hint = EXCLUDED.token_hint,
            token_updated_at = NOW(),
            enabled = TRUE,
            status = 'unknown',
            status_error = ''
        RETURNING {_COLUMNS}
        """,
        {"id": str(group_id), "title": title, "photo": photo_url, "token": token_encrypted, "hint": token_hint},
    )
    return await cursor.fetchone()


async def replace_token(
    conn: AsyncConnection, group_id: int, *, title: str, photo_url: str, token_encrypted: str, token_hint: str
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        f"""
        UPDATE channels SET
            title = CASE WHEN %(title)s <> '' THEN %(title)s ELSE title END,
            photo_url = %(photo)s,
            token_encrypted = %(token)s,
            token_hint = %(hint)s,
            token_updated_at = NOW(),
            status = 'unknown',
            status_error = ''
        WHERE {_VK} AND external_id = %(id)s
        RETURNING {_COLUMNS}
        """,
        {"id": str(group_id), "title": title, "photo": photo_url, "token": token_encrypted, "hint": token_hint},
    )
    return await cursor.fetchone()


async def set_enabled(conn: AsyncConnection, group_id: int, enabled: bool) -> dict[str, Any] | None:
    cursor = await conn.execute(
        f"UPDATE channels SET enabled = %s WHERE {_VK} AND external_id = %s RETURNING {_COLUMNS}",
        (enabled, str(group_id)),
    )
    return await cursor.fetchone()


async def enabled_tokens(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Включённые сообщества с ключом: group_id, title, token_encrypted."""
    cursor = await conn.execute(
        f"""
        SELECT external_id::bigint AS group_id, title, token_encrypted FROM channels
        WHERE {_VK} AND enabled AND token_encrypted IS NOT NULL
        ORDER BY external_id::bigint
        """
    )
    return list(await cursor.fetchall())


async def disabled_ids(conn: AsyncConnection) -> list[int]:
    cursor = await conn.execute(
        f"SELECT external_id::bigint AS group_id FROM channels WHERE {_VK} AND NOT enabled ORDER BY 1"
    )
    return [int(row["group_id"]) for row in await cursor.fetchall()]


async def import_token(
    conn: AsyncConnection, group_id: int, *, title: str, token_encrypted: str, token_hint: str
) -> bool:
    """Сохраняет ключ из окружения бота, только если сообщества нет или у него нет ключа.

    enabled и статус не трогает. True, если ключ записан.
    """
    cursor = await conn.execute(
        """
        INSERT INTO channels (platform, external_id, title, token_encrypted, token_hint, token_updated_at)
        VALUES ('vk', %(id)s, %(title)s, %(token)s, %(hint)s, NOW())
        ON CONFLICT (platform, external_id) DO UPDATE SET
            title = CASE WHEN channels.title = '' THEN EXCLUDED.title ELSE channels.title END,
            token_encrypted = EXCLUDED.token_encrypted,
            token_hint = EXCLUDED.token_hint,
            token_updated_at = NOW()
        WHERE channels.token_encrypted IS NULL
        RETURNING id
        """,
        {"id": str(group_id), "title": title, "token": token_encrypted, "hint": token_hint},
    )
    return await cursor.fetchone() is not None


async def report_status(conn: AsyncConnection, items: list[tuple[int, str, str]]) -> None:
    """Статус работы бота по сообществам. Несуществующие сообщества игнорируются."""
    for group_id, status, error in items:
        await conn.execute(
            f"""
            UPDATE channels SET status = %s, status_error = %s, last_seen_at = NOW()
            WHERE {_VK} AND external_id = %s
            """,
            (status, error[:500], str(group_id)),
        )
