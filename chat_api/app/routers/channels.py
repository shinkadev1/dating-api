"""Чаты, куда подключён бот: сообщества ВК, бот Telegram, сайт."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.deps import CLIENT_PLATFORMS, Chat, CurrentClient
from app.schemas import ChannelIn, NoticeIn, NoticeOut

router = APIRouter(
    tags=["channels"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токену недоступна эта платформа"},
    },
)


@router.post("/channels", status_code=status.HTTP_204_NO_CONTENT, summary="Зарегистрировать чат")
async def register_channel(body: ChannelIn, client: CurrentClient, chat: Chat) -> None:
    """Создаёт чат или обновляет название. Пустое название уже записанное не затирает."""
    if body.platform not in CLIENT_PLATFORMS[client]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Токену недоступна платформа {body.platform}")
    await chat.register_channel(body.platform, body.external_id, body.title)


@router.post("/notifications", summary="Уведомить все чаты бота")
async def notify_channels(body: NoticeIn, client: CurrentClient, chat: Chat) -> NoticeOut:
    """Ставит текст в очередь доставки по всем чатам, куда подключён бот.

    Чат — сообщество ВК, бот Telegram или сайт. Сообщение уходит каждому человеку,
    который уже писал боту в этом чате. Если человек открыл два сообщества,
    он получит текст в обоих: из чужого сообщества ВК написать нельзя.
    Нужен сервисный токен бота. Текущий анонимный диалог не закрывается,
    а доставка идёт отдельно от живых реплик.
    """
    if client != "bot":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен сервисный токен бота")
    return NoticeOut(queued=await chat.notify_channels(body.text))
