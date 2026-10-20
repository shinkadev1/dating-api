"""Клиент chat-engine: REST для команд и WebSocket для событий аккаунта."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.asyncio.client import connect

# Сообщение длиннее не примет ни chat-engine, ни Chat API.
MAX_TEXT_LENGTH = 4096


class EngineError(Exception):
    """chat-engine недоступен или ответил ошибкой."""


class EngineConflictError(EngineError):
    """Действие недоступно в текущем состоянии аккаунта."""


@dataclass(frozen=True)
class EngineAccount:
    id: str
    authorized: bool
    state: str | None


class EngineClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        self._base_url = base_url
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=f"{base_url}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise EngineError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == httpx.codes.CONFLICT:
            raise EngineConflictError(response.text)
        if response.is_error:
            raise EngineError(f"{method} {path}: HTTP {response.status_code} {response.text}")
        return response.json()

    async def accounts(self) -> list[EngineAccount]:
        body = await self._request("GET", "/accounts")
        return [EngineAccount(str(item["id"]), bool(item["authorized"]), item.get("state")) for item in body]

    async def start(self, account: str) -> None:
        await self._request("POST", f"/accounts/{account}/chat/start")

    async def stop(self, account: str) -> None:
        await self._request("POST", f"/accounts/{account}/chat/stop")

    async def send(self, account: str, text: str) -> None:
        await self._request("POST", f"/accounts/{account}/chat/messages", {"text": text[:MAX_TEXT_LENGTH]})

    async def send_photo(self, account: str, data: bytes, content_type: str) -> None:
        try:
            response = await self._http.post(
                f"/accounts/{account}/chat/photos",
                files={"file": ("photo.jpg", data, content_type or "image/jpeg")},
                timeout=90,
            )
        except httpx.HTTPError as exc:
            raise EngineError(f"POST photo: {type(exc).__name__}: {exc}") from exc
        if response.status_code == httpx.codes.CONFLICT:
            raise EngineConflictError(response.text)
        if response.is_error:
            raise EngineError(f"POST photo: HTTP {response.status_code} {response.text}")

    async def fetch_photo(self, account: str, photo_id: str) -> tuple[bytes, str]:
        try:
            response = await self._http.get(
                f"/accounts/{account}/chat/photos/{photo_id}",
                timeout=90,
            )
        except httpx.HTTPError as exc:
            raise EngineError(f"GET photo: {type(exc).__name__}: {exc}") from exc
        if response.is_error:
            raise EngineError(f"GET photo: HTTP {response.status_code} {response.text}")
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        return response.content, content_type or "image/jpeg"

    async def events(self, account: str) -> AsyncIterator[dict]:
        """События аккаунта: сначала snapshot, затем state и message. Заканчивается при обрыве."""
        url = f"{self._base_url.replace('http', 'ws', 1)}/api/v1/accounts/{account}/chat/ws"
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            async for raw in socket:
                yield json.loads(raw)
