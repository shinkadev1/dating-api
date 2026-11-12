"""Подбор собеседника из chat-engine, когда живого в очереди нет. Вызывает только engine_bridge.

Сообщения, остановка и состояние аккаунта идут через обычные `/peers/engine/{account}/1/...`.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, status

from app.deps import Chat, EnginePeer, require_engine
from app.schemas import EngineAttachOut, EngineDemandOut, SearchIn, StateOut

router = APIRouter(
    prefix="/engine",
    tags=["engine"],
    dependencies=[Depends(require_engine)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токен не engine_bridge"},
    },
)


@router.get("/demand", summary="Сколько людей ждут собеседника")
async def demand(chat: Chat) -> EngineDemandOut:
    """Люди в очереди дольше `ENGINE_FALLBACK_AFTER_SECONDS` и аккаунты chat-engine, которые уже ищут."""
    return await chat.engine_demand()


@router.post("/{account}/hunt", summary="Аккаунт chat-engine начал поиск")
async def hunt(engine: EnginePeer, chat: Chat, body: SearchIn | None = Body(default=None)) -> StateOut:
    """Отмечает, что аккаунт ищет собеседника в chat-engine. Живые люди его не находят.

    Тело `{"mode": "normal" | "adult"}` — режим аккаунта; без тела `normal`.
    """
    return await chat.engine_hunt(engine, body.mode if body else "normal")


@router.post("/{account}/attach", summary="Отдать найденного собеседника человеку")
async def attach(engine: EnginePeer, chat: Chat) -> EngineAttachOut:
    """Связывает аккаунт с человеком его режима (режим задан в `hunt`), который ждёт дольше всех. Человек получает `partner_found`.

    `attached=false`, если ждущих людей не осталось: тогда разговор в chat-engine нужно закрыть.
    """
    return await chat.engine_attach(engine)
