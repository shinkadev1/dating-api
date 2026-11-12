"""SQL сообществ ВК, управляемых из CRM: ключи (открытым текстом), переключатель и статус."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

# external_id сообщества ВК — положительное число; всё остальное в таблице игнорируется.
_VK = "platform = 'vk' AND external_id ~ '^[0-9]+$'"

_COLUMNS = """
    external_id::bigint AS group_id, title, photo_url, token_hint, (token IS NOT NULL) AS has_token,
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
    conn: AsyncConnection, group_id: int, *, title: str, photo_url: str, token: str, token_hint: str
) -> dict[str, Any]:
    """Создаёт сообщество или обновляет существующее: новый ключ, включено, статус неизвестен."""
    cursor = await conn.execute(
        f"""
        INSERT INTO channels (platform, external_id, title, photo_url, token, token_encrypted, token_hint, token_updated_at)
        VALUES ('vk', %(id)s, %(title)s, %(photo)s, %(token)s, NULL, %(hint)s, NOW())
        ON CONFLICT (platform, external_id) DO UPDATE SET
            title = CASE WHEN EXCLUDED.title <> '' THEN EXCLUDED.title ELSE channels.title END,
            photo_url = EXCLUDED.photo_url,
            token = EXCLUDED.token,
            token_encrypted = NULL,
            token_hint = EXCLUDED.token_hint,
            token_updated_at = NOW(),
            enabled = TRUE,
            status = 'unknown',
            status_error = ''
        RETURNING {_COLUMNS}
        """,
        {"id": str(group_id), "title": title, "photo": photo_url, "token": token, "hint": token_hint},
    )
    return await cursor.fetchone()


async def replace_token(
    conn: AsyncConnection, group_id: int, *, title: str, photo_url: str, token: str, token_hint: str
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        f"""
        UPDATE channels SET
            title = CASE WHEN %(title)s <> '' THEN %(title)s ELSE title END,
            photo_url = %(photo)s,
            token = %(token)s,
            token_encrypted = NULL,
            token_hint = %(hint)s,
            token_updated_at = NOW(),
            status = 'unknown',
            status_error = ''
        WHERE {_VK} AND external_id = %(id)s
        RETURNING {_COLUMNS}
        """,
        {"id": str(group_id), "title": title, "photo": photo_url, "token": token, "hint": token_hint},
    )
    return await cursor.fetchone()


async def set_enabled(conn: AsyncConnection, group_id: int, enabled: bool) -> dict[str, Any] | None:
    cursor = await conn.execute(
        f"UPDATE channels SET enabled = %s WHERE {_VK} AND external_id = %s RETURNING {_COLUMNS}",
        (enabled, str(group_id)),
    )
    return await cursor.fetchone()


async def enabled_tokens(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Включённые сообщества с ключом: group_id, title, token."""
    cursor = await conn.execute(
        f"""
        SELECT external_id::bigint AS group_id, title, token FROM channels
        WHERE {_VK} AND enabled AND token IS NOT NULL
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
    conn: AsyncConnection, group_id: int, *, title: str, token: str, token_hint: str
) -> bool:
    """Сохраняет ключ из окружения бота, только если сообщества нет или у него нет ключа.

    enabled и статус не трогает. True, если ключ записан.
    """
    cursor = await conn.execute(
        """
        INSERT INTO channels (platform, external_id, title, token, token_hint, token_updated_at)
        VALUES ('vk', %(id)s, %(title)s, %(token)s, %(hint)s, NOW())
        ON CONFLICT (platform, external_id) DO UPDATE SET
            title = CASE WHEN channels.title = '' THEN EXCLUDED.title ELSE channels.title END,
            token = EXCLUDED.token,
            token_encrypted = NULL,
            token_hint = EXCLUDED.token_hint,
            token_updated_at = NOW()
        WHERE channels.token IS NULL
        RETURNING id
        """,
        {"id": str(group_id), "title": title, "token": token, "hint": token_hint},
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


async def encrypted_rows(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Старые записи с зашифрованным ключом, который ещё не перенесён в token."""
    cursor = await conn.execute(
        f"""
        SELECT external_id::bigint AS group_id, token_encrypted FROM channels
        WHERE {_VK} AND token IS NULL AND token_encrypted LIKE 'v1:%'
        ORDER BY external_id::bigint
        """
    )
    return list(await cursor.fetchall())


async def set_plain(conn: AsyncConnection, group_id: int, token: str, old_encrypted: str) -> bool:
    """Переносит ключ в token и обнуляет token_encrypted. Не затирает запись, изменённую за это время."""
    cursor = await conn.execute(
        f"""
        UPDATE channels SET token = %s, token_encrypted = NULL
        WHERE {_VK} AND external_id = %s AND token IS NULL AND token_encrypted = %s
        RETURNING id
        """,
        (token, str(group_id), old_encrypted),
    )
    return await cursor.fetchone() is not None
