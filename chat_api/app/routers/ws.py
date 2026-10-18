"""WebSocket событий: платформа web для приложения сайта, vk и tg для ботов на других серверах."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.config import get_settings
from app.deps import CLIENT_PLATFORMS, identify_client
from app.events import BotEventFeed, broadcast_stream

router = APIRouter(tags=["events"])

_BOT_PLATFORMS = frozenset({"vk", "tg"})


def _bearer_token(websocket: WebSocket) -> str | None:
    scheme, _, token = (websocket.headers.get("authorization") or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


@router.websocket("/events/web")
async def web_events(websocket: WebSocket) -> None:
    """Все события пользователей сайта. Приложение само раздаёт их по своим пользователям.

    Требует заголовок `Authorization: Bearer <токен приложения>`, иначе
    соединение закрывается с кодом 1008. Сервер только отправляет JSON
    `{"to": {"platform", "bot_key", "user_id"}, "type", "text"?, "reason"?}`.
    `type=notice` — уведомление всем диалогам, его не нужно показывать как реплику собеседника.
    Входящие сообщения клиента игнорируются.
    """
    if identify_client(_bearer_token(websocket), get_settings()) != "app":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    hub = websocket.app.state.hub
    hub.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(websocket)


async def _serve_bot_feed(
    websocket: WebSocket,
    platform: str,
    consumer: str,
    *,
    stream: str | None = None,
    start: str = "$",
) -> None:
    client = identify_client(_bearer_token(websocket), get_settings())
    if platform not in _BOT_PLATFORMS or client is None or platform not in CLIENT_PLATFORMS[client]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    await BotEventFeed(websocket.app.state.redis, platform, consumer, stream=stream, start=start).serve(websocket)


@router.websocket("/events/{platform}")
async def bot_events(
    websocket: WebSocket,
    platform: str,
    consumer: Annotated[str, Query(pattern=r"^[\w.-]{1,64}$")] = "bot",
) -> None:
    """События пользователей бота платформы vk или tg с подтверждением доставки.

    Требует `Authorization: Bearer <токен бота>`, которому доступна платформа, иначе код 1008.
    Сервер шлёт `{"id", "event": {"to", "type", "text"?, "reason"?}}`, клиент после доставки
    отвечает `{"ack": id}`. Неподтверждённое событие придёт снова при переподключении
    с тем же `consumer`.
    """
    await _serve_bot_feed(websocket, platform, consumer)


@router.websocket("/broadcasts/{platform}")
async def broadcast_events(
    websocket: WebSocket,
    platform: str,
    consumer: Annotated[str, Query(pattern=r"^[\w.-]{1,64}$")] = "bot",
) -> None:
    """Уведомления диалогам бота. Тот же протокол, что у `/events/{platform}`, событие `notice`.

    Отдельный сокет, чтобы рассылка не задерживала реплики анонимного диалога.
    """
    await _serve_bot_feed(websocket, platform, consumer, stream=broadcast_stream(platform), start="0")
