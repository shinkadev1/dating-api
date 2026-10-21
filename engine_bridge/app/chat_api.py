"""Клиент Chat API от имени аккаунтов chat-engine (платформа engine)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

ENGINE_PLATFORM = "engine"
ENGINE_USER_ID = 1


class ChatApiError(Exception):
    """API недоступен или ответил ошибкой."""


class NotChattingError(ChatApiError):
    """У аккаунта уже нет активного диалога."""


@dataclass(frozen=True)
class Demand:
    waiting: int
    hunting: int


class ChatApiClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise ChatApiError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == httpx.codes.CONFLICT:
            raise NotChattingError(response.text)
        if response.is_error:
            raise ChatApiError(f"{method} {path}: HTTP {response.status_code} {response.text}")
        if response.status_code == httpx.codes.NO_CONTENT:
            return None
        return response.json()

    @staticmethod
    def _peer(account: str, action: str) -> str:
        return f"/peers/{ENGINE_PLATFORM}/{account}/{ENGINE_USER_ID}/{action}"

    async def visit(self, account: str) -> None:
        await self._request("POST", self._peer(account, "visit"))

    async def phase(self, account: str) -> str:
        return (await self._request("GET", self._peer(account, "state")))["phase"]

    async def stop(self, account: str) -> None:
        await self._request("POST", self._peer(account, "stop"))

    async def send_message(self, account: str, text: str) -> bool:
        """True, если сообщение ушло человеку."""
        return (await self._request("POST", self._peer(account, "messages"), {"text": text}))["delivered"]

    async def send_photo(self, account: str, data: bytes, content_type: str) -> bool:
        """Кладёт фото в диалог человека. True, если событие поставлено."""
        return await self._send_media(account, "photo", data, content_type or "image/jpeg", "photo.jpg")

    async def send_video(self, account: str, data: bytes, content_type: str) -> bool:
        """Кладёт видео в диалог человека. True, если событие поставлено."""
        return await self._send_media(account, "video", data, content_type or "video/mp4", "video.mp4")

    async def _send_media(
        self,
        account: str,
        kind: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> bool:
        try:
            response = await self._http.post(
                self._peer(account, "media"),
                data={"kind": kind},
                files={"file": (filename, data, content_type)},
                timeout=180,
            )
        except httpx.HTTPError as exc:
            raise ChatApiError(f"POST media: {type(exc).__name__}: {exc}") from exc
        if response.status_code == httpx.codes.CONFLICT:
            raise NotChattingError(response.text)
        if response.is_error:
            raise ChatApiError(f"POST media: HTTP {response.status_code} {response.text}")
        return True

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Байты файла, который человек отправил аккаунту chat-engine."""
        try:
            response = await self._http.get(f"/media/{media_id}", timeout=90)
        except httpx.HTTPError as exc:
            raise ChatApiError(f"GET media: {type(exc).__name__}: {exc}") from exc
        if response.is_error:
            raise ChatApiError(f"GET media: HTTP {response.status_code} {response.text}")
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        return response.content, content_type or "image/jpeg"

    async def demand(self) -> Demand:
        body = await self._request("GET", "/engine/demand")
        return Demand(waiting=int(body["waiting"]), hunting=int(body["hunting"]))

    async def hunt(self, account: str) -> None:
        await self._request("POST", f"/engine/{account}/hunt")

    async def attach(self, account: str) -> bool:
        """True, если найденный собеседник отдан ждущему человеку."""
        return (await self._request("POST", f"/engine/{account}/attach"))["attached"]
