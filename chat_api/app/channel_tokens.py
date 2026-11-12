"""Ключи сообществ ВК: подсказка для CRM и однократный перенос старых зашифрованных записей.

Новые ключи хранятся в `channels.token` открытым текстом. Раньше они лежали в `channels.token_encrypted`
как `v1:` + base64(nonce[12] + шифртекст), AES-256-GCM, ключ — SHA-256(CHANNEL_TOKEN_KEY),
AAD = `vk:<group_id>`. Расшифровка нужна только для переноса.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg_pool import AsyncConnectionPool

from app.repositories import channels as repo

logger = logging.getLogger(__name__)

PREFIX = "v1:"
_NONCE_BYTES = 12


class TokenDecryptError(Exception):
    """Значение не расшифровалось: другой ключ, другое сообщество или повреждённые данные."""


def _aead(secret: str) -> AESGCM:
    return AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())


def _aad(group_id: int) -> bytes:
    return f"vk:{int(group_id)}".encode()


def decrypt(secret: str, group_id: int, value: str) -> str:
    if not value.startswith(PREFIX):
        raise TokenDecryptError("неизвестный формат")
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
        if len(raw) <= _NONCE_BYTES:
            raise ValueError("короткое значение")
        plain = _aead(secret).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(group_id))
        return plain.decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise TokenDecryptError("не удалось расшифровать") from exc


def hint(token: str) -> str:
    """Подсказка для CRM: первые 6 знаков, многоточие, последние 4. Короткий ключ не раскрывается."""
    if len(token) < 16:
        return "…"
    return f"{token[:6]}…{token[-4:]}"


async def migrate_encrypted(pool: AsyncConnectionPool, secret: str | None) -> int:
    """Переносит старые зашифрованные ключи в `token`. Не падает; значения ключей в лог не попадают."""
    moved = 0
    try:
        async with pool.connection() as conn:
            rows = await repo.encrypted_rows(conn)
            if not rows:
                return 0
            if not secret:
                logger.warning(
                    "CHANNEL_TOKEN_KEY не задан: не перенесены ключи сообществ ВК: %s",
                    ", ".join(str(r["group_id"]) for r in rows),
                )
                return 0
            for row in rows:
                group_id = int(row["group_id"])
                try:
                    plain = decrypt(secret, group_id, row["token_encrypted"])
                except TokenDecryptError:
                    logger.error("Ключ сообщества ВК %s не расшифровался: пропускаю", group_id)
                    continue
                if await repo.set_plain(conn, group_id, plain, row["token_encrypted"]):
                    moved += 1
    except Exception:
        logger.exception("Перенос ключей сообществ ВК не удался")
        return moved
    if moved:
        logger.info("Перенесено токенов сообществ ВК: %d", moved)
    return moved
