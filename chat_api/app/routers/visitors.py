"""Посетители сайта: кука и строка в bot_users."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.deps import Chat, CurrentClient
from app.schemas import VisitorIn, VisitorOut

router = APIRouter(prefix="/visitors", tags=["visitors"])


@router.post("", summary="Запомнить посетителя сайта")
async def save_visitor(body: VisitorIn, chat: Chat, client: CurrentClient) -> VisitorOut:
    """Создаёт или обновляет человека по куке сайта.

    Токен только у сайта. `user_id` передаёт воркер, когда браузер уже вошёл в чат
    и куку нужно привязать к этому гостю, а не заводить второго.
    """
    if client != "app":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен токен сайта")
    return await chat.save_web_visitor(body.id, body.bot_key, body.user_id)
