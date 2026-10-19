"""Зависимости маршрутов: сервис чата, клиент API и адресат запроса."""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.peer import ENGINE_PLATFORM, ENGINE_USER_ID, Peer
from app.services.chat import ChatService
from app.services.media import MediaService

Client = Literal["bot", "tg_bot", "app", "engine"]
# Бот ВК и Telegram не может действовать от имени пользователей сайта и наоборот.
# Бот Telegram на отдельном сервере — только за пользователей tg,
# engine_bridge — только за аккаунты chat-engine.
CLIENT_PLATFORMS: dict[Client, frozenset[str]] = {
    "bot": frozenset({"vk", "tg"}),
    "tg_bot": frozenset({"tg"}),
    "app": frozenset({"web"}),
    "engine": frozenset({ENGINE_PLATFORM}),
}

_bearer = HTTPBearer(auto_error=False)


def get_chat(request: Request) -> ChatService:
    return request.app.state.chat


def get_media(request: Request) -> MediaService:
    return request.app.state.media


Chat = Annotated[ChatService, Depends(get_chat)]
MediaSvc = Annotated[MediaService, Depends(get_media)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _matches(token: str, expected: str) -> bool:
    return hmac.compare_digest(token.encode(), expected.encode())


def identify_client(token: str | None, settings: Settings) -> Client | None:
    """Определяет клиента по токену. None, если токен не подходит."""
    if not token:
        return None
    if _matches(token, settings.chat_api_service_token):
        return "bot"
    if _matches(token, settings.chat_api_app_token):
        return "app"
    if settings.chat_api_engine_token and _matches(token, settings.chat_api_engine_token):
        return "engine"
    if settings.chat_api_tg_token and _matches(token, settings.chat_api_tg_token):
        return "tg_bot"
    return None


def current_client(credentials: Credentials, settings: AppSettings) -> Client:
    client = identify_client(credentials.credentials if credentials else None, settings)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Нужен действующий токен",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return client


CurrentClient = Annotated[Client, Depends(current_client)]


def service_peer(
    platform: Annotated[Literal["vk", "tg", "web", "engine"], Path(description="Платформа пользователя")],
    bot_key: Annotated[
        str, Path(min_length=1, max_length=64, description="Бот или сайт, через который пишет пользователь")
    ],
    user_id: Annotated[int, Path(gt=0, description="ID пользователя на платформе")],
    client: CurrentClient,
) -> Peer:
    """Клиент действует от имени пользователя только своей платформы."""
    if platform not in CLIENT_PLATFORMS[client]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Токену недоступна платформа {platform}")
    return Peer(platform, bot_key, user_id)


ServicePeer = Annotated[Peer, Depends(service_peer)]


def human_peer(peer: ServicePeer) -> Peer:
    """Встать в общую очередь может только человек: аккаунту chat-engine пару даёт attach."""
    if peer.is_engine:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Аккаунт chat-engine ищет через /engine/{account}/hunt")
    return peer


HumanPeer = Annotated[Peer, Depends(human_peer)]


def require_engine(client: CurrentClient) -> None:
    if client != "engine":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен токен engine_bridge")


def engine_peer(
    account: Annotated[str, Path(min_length=1, max_length=64, description="ID аккаунта в chat-engine")],
) -> Peer:
    return Peer(ENGINE_PLATFORM, account, ENGINE_USER_ID)


EnginePeer = Annotated[Peer, Depends(engine_peer)]
