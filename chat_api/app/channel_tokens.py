"""Шифрование ключей сообществ ВК перед записью в базу: AES-256-GCM.

Ключ шифрования — SHA-256(CHANNEL_TOKEN_KEY). Формат значения: `v1:` + base64(nonce[12] + шифртекст).
В AAD входит `vk:<group_id>`, поэтому значение нельзя перенести в строку другого сообщества.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"
_NONCE_BYTES = 12


class TokenDecryptError(Exception):
    """Значение не расшифровалось: другой ключ, другое сообщество или повреждённые данные."""


def _aead(secret: str) -> AESGCM:
    return AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())


def _aad(group_id: int) -> bytes:
    return f"vk:{int(group_id)}".encode()


def encrypt(secret: str, group_id: int, token: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _aead(secret).encrypt(nonce, token.encode("utf-8"), _aad(group_id))
    return PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


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
