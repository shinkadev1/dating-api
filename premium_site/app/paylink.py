"""Подписанная ссылка на оплату премиума: токен в utm_content.

Копия этого модуля лежит в chat_api/app/premium_link.py — формат должен совпадать.
Токен — `1` + base64url(nonce | шифротекст | метка). Внутри `platform:user_id:issued:expires`,
зашифрованные потоком HMAC-SHA256 и подписанные HMAC-SHA256 (encrypt-then-MAC).
Поэтому в адресе нет открытого id пользователя, а подделать или продлить токен без
общего секрета PREMIUM_LINK_SECRET нельзя. Только стандартная библиотека.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

VERSION = "1"
TTL_SECONDS = 24 * 3600
PLATFORMS = frozenset({"vk", "tg", "web"})
_NONCE = 12
_TAG = 16
_MAX_TOKEN = 256


class InvalidLinkToken(ValueError):
    """Токен повреждён, подписан другим ключом или истёк."""


def _keys(secret: str) -> tuple[bytes, bytes]:
    raw = secret.encode()
    return (
        hmac.new(raw, b"premium-link:enc", hashlib.sha256).digest(),
        hmac.new(raw, b"premium-link:mac", hashlib.sha256).digest(),
    )


def _stream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < size:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:size]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def sign(
    platform: str,
    user_id: int,
    secret: str,
    *,
    now: int | None = None,
    ttl: int = TTL_SECONDS,
    nonce: bytes | None = None,
) -> str:
    if platform not in PLATFORMS:
        raise ValueError(f"Платформа {platform} не покупает премиум")
    if user_id <= 0:
        raise ValueError("Некорректный ID")
    if not secret:
        raise ValueError("Пустой секрет")
    issued = int(time.time()) if now is None else now
    nonce = nonce if nonce is not None else os.urandom(_NONCE)
    if len(nonce) != _NONCE:
        raise ValueError("Неверная длина nonce")
    enc_key, mac_key = _keys(secret)
    plain = f"{platform}:{user_id}:{issued}:{issued + ttl}".encode()
    body = nonce + _xor(plain, _stream(enc_key, nonce, len(plain)))
    tag = hmac.new(mac_key, VERSION.encode() + body, hashlib.sha256).digest()[:_TAG]
    return VERSION + base64.urlsafe_b64encode(body + tag).rstrip(b"=").decode()


def verify(token: str, secret: str, *, now: int | None = None) -> tuple[str, int]:
    """Возвращает (platform, user_id) или бросает InvalidLinkToken."""
    if not secret or not token or len(token) > _MAX_TOKEN or not token.startswith(VERSION):
        raise InvalidLinkToken("Неверный формат")
    encoded = token[len(VERSION) :]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise InvalidLinkToken("Неверный формат") from exc
    if len(raw) <= _NONCE + _TAG:
        raise InvalidLinkToken("Неверный формат")
    body, tag = raw[:-_TAG], raw[-_TAG:]
    enc_key, mac_key = _keys(secret)
    expected = hmac.new(mac_key, VERSION.encode() + body, hashlib.sha256).digest()[:_TAG]
    if not hmac.compare_digest(tag, expected):
        raise InvalidLinkToken("Неверная подпись")
    nonce, cipher = body[:_NONCE], body[_NONCE:]
    try:
        platform, user, issued, expires = _xor(cipher, _stream(enc_key, nonce, len(cipher))).decode().split(":")
        user_id, issued_at, expires_at = int(user), int(issued), int(expires)
    except ValueError as exc:
        raise InvalidLinkToken("Неверное содержимое") from exc
    current = int(time.time()) if now is None else now
    if platform not in PLATFORMS or user_id <= 0:
        raise InvalidLinkToken("Неверное содержимое")
    if issued_at > current + 300 or expires_at <= current:
        raise InvalidLinkToken("Срок ссылки истёк")
    return platform, user_id
