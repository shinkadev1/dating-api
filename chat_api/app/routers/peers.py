"""Действия пользователя в чате.

Бот ходит за vk и tg, приложение сайта — за web, engine_bridge — за аккаунты chat-engine
(кроме поиска и жалобы: их аккаунту заменяют `/engine/{account}/hunt` и `/attach`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile, status

from app.deps import Chat, HumanPeer, MediaSvc, ServicePeer
from app.media_types import MAX_BYTES, MediaRejected
from app.schemas import MediaKind, MediaOut, MessageIn, MessageOut, StateOut, StopOut, VisitIn
from app.services.chat import NotChattingError
from app.services.media import StorageDisabled
from app.services.storage import StorageError

_NOT_CHATTING = "Нет активного диалога"

router = APIRouter(
    prefix="/peers/{platform}/{bot_key}/{user_id}",
    tags=["peers"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токену недоступна эта платформа"},
    },
)


@router.post("/visit", status_code=status.HTTP_204_NO_CONTENT, summary="Отметить визит")
async def visit(peer: ServicePeer, chat: Chat, body: VisitIn | None = Body(default=None)) -> None:
    """Запоминает пользователя и чат, через который он зашёл, и обновляет время визита.

    Для Telegram в теле можно передать `handle` — `@username`. Пустое значение
    уже сохранённое имя не затирает.
    """
    await chat.visit(peer, body.handle if body else None)


@router.get("/state", summary="Текущее состояние")
async def state(peer: ServicePeer, chat: Chat) -> StateOut:
    """Фаза пользователя: `idle`, `searching` или `chatting`. Незнакомый пользователь — `idle`."""
    return await chat.state(peer)


@router.post("/search", summary="Искать собеседника")
async def search(peer: HumanPeer, chat: Chat) -> StateOut:
    """Ставит пользователя в очередь поиска.

    Если в очереди уже кто-то есть, пара создаётся сразу: ответ `chatting`,
    собеседник получает событие `partner_found`. Иначе ответ `searching`,
    а `partner_found` придёт пользователю событием, когда найдётся пара.
    Текущий диалог при этом закрывается, бывший собеседник получает
    `partner_left` с причиной `stopped`.
    """
    return await chat.search(peer)


@router.post("/stop", summary="Закончить диалог или поиск")
async def stop(peer: ServicePeer, chat: Chat) -> StopOut:
    """Переводит пользователя в `idle`.

    `was_active` показывает, был ли активный поиск или диалог. Собеседник
    получает `partner_left` с причиной `stopped`.
    """
    return await chat.stop(peer)


@router.post(
    "/report",
    summary="Пожаловаться на собеседника",
    responses={status.HTTP_409_CONFLICT: {"description": _NOT_CHATTING}},
)
async def report(peer: HumanPeer, chat: Chat) -> StateOut:
    """Закрывает диалог и сразу ставит пользователя в поиск, как `/search`.

    Собеседник получает `partner_left` с причиной `reported`.
    """
    try:
        return await chat.report(peer)
    except NotChattingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_CHATTING) from exc


@router.post(
    "/messages",
    summary="Отправить сообщение",
    responses={status.HTTP_409_CONFLICT: {"description": _NOT_CHATTING}},
)
async def send_message(body: MessageIn, peer: ServicePeer, chat: Chat) -> MessageOut:
    """Пересылает текст собеседнику событием `message`.

    Если собеседник уже вышел из пары, сообщение не доставляется: диалог
    закрывается, ответ `delivered=false` и `phase=idle`.
    """
    try:
        return await chat.send_message(peer, body.text)
    except NotChattingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_CHATTING) from exc


@router.post(
    "/media",
    summary="Сохранить медиа",
    responses={
        status.HTTP_409_CONFLICT: {"description": _NOT_CHATTING},
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {"description": "Файл больше предела для этого вида"},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Содержимое не совпало с видом"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Бакет не принял файл"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "S3 не настроен"},
    },
)
async def store_media(
    peer: ServicePeer,
    chat: Chat,
    media: MediaSvc,
    kind: Annotated[MediaKind, Form(description="photo, video, video_note или voice")],
    file: Annotated[UploadFile, File(description="Байты фото, видео, кружка или голосового")],
) -> MediaOut:
    """Кладёт файл в S3 и запоминает ключ. Собеседнику медиа этим запросом не отправляется.

    Принимается только в активном диалоге. Вид проверяется по байтам файла, а не по имени.
    """
    if (await chat.state(peer)).phase != "chatting":
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_CHATTING)
    raw = await file.read(MAX_BYTES[kind] + 1)
    try:
        return await media.save(peer, kind, raw)
    except StorageDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Хранилище медиа не настроено") from exc
    except MediaRejected as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось сохранить файл") from exc


@router.post("/delivery-failed", status_code=status.HTTP_204_NO_CONTENT, summary="Сообщить о недоставке")
async def delivery_failed(peer: ServicePeer, chat: Chat) -> None:
    """Клиент не смог доставить событие этому пользователю, например он заблокировал бота.

    Поиск или диалог пользователя закрывается, собеседник получает
    `partner_left` с причиной `undeliverable`.
    """
    await chat.delivery_failed(peer)
