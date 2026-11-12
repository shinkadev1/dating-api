"""Проверка ключа сообщества ВК: groups.getById и groups.getLongPollSettings. Ключ не логируется."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API_URL = "https://api.vk.com/method"
API_VERSION = "5.199"
TIMEOUT = 10.0


class VkRejected(Exception):
    """ВК отклонил ключ."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


class VkUnavailable(Exception):
    """ВК не ответил или ответил не по протоколу."""


@dataclass(frozen=True)
class GroupInfo:
    group_id: int
    title: str
    photo_url: str
    long_poll: bool


async def _call(client: httpx.AsyncClient, method: str, token: str, **params: object) -> dict:
    try:
        response = await client.post(
            f"{API_URL}/{method}",
            data={"access_token": token, "v": API_VERSION, **params},
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # В тексте httpx-ошибки может быть адрес запроса; ключ в нём не передаётся (он в теле), но детали не нужны.
        raise VkUnavailable(type(exc).__name__) from None
    if not isinstance(payload, dict):
        raise VkUnavailable("неожиданный ответ")
    error = payload.get("error")
    if isinstance(error, dict):
        try:
            code = int(error.get("error_code") or 0)
        except (TypeError, ValueError):
            code = 0
        raise VkRejected(code, str(error.get("error_msg") or ""))
    if "response" not in payload:
        raise VkUnavailable("неожиданный ответ")
    return payload["response"]


async def check_token(token: str) -> GroupInfo:
    """Возвращает сообщество, которому принадлежит ключ. Ничего не записывает."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        raw = await _call(client, "groups.getById", token, fields="")
        try:
            group = raw[0] if isinstance(raw, list) else raw["groups"][0]
            group_id = int(group["id"])
            title = str(group.get("name") or "")
            photo_url = str(group.get("photo_200") or "")
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            raise VkUnavailable("неожиданный ответ") from None
        settings = await _call(client, "groups.getLongPollSettings", token, group_id=group_id)
    return GroupInfo(
        group_id=group_id,
        title=title,
        photo_url=photo_url,
        long_poll=bool(isinstance(settings, dict) and settings.get("is_enabled")),
    )
