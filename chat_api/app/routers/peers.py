"""Действия пользователя в чате.

Бот ходит за vk и tg, приложение сайта — за web, engine_bridge — за аккаунты chat-engine
(кроме поиска и жалобы: их аккаунту заменяют `/engine/{account}/hunt` и `/attach`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from app import premium_link
from app.deps import AppSettings, Chat, HumanPeer, MediaSvc, ServicePeer
from app.media_types import MAX_BYTES, MediaRejected
from app.schemas import (
    MediaKind,
    MediaOut,
    MessageIn,
    MessageOut,
    PremiumLinkOut,
    StateOut,
    StopOut,
    TgSend,
    VisitIn,
)
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
        status.HTTP_204_NO_CONTENT: {"description": "Байтов нет, собеседнику в Telegram ушёл file_id"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Нет премиума у отправителя или у живого собеседника; detail содержит «премиум»"
        },
        status.HTTP_409_CONFLICT: {"description": _NOT_CHATTING},
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {"description": "Файл больше предела для этого вида"},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Содержимое не совпало с видом"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Бакет не принял файл"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "S3 не настроен"},
    },
    response_model=MediaOut,
)
async def store_media(
    peer: ServicePeer,
    chat: Chat,
    media: MediaSvc,
    kind: Annotated[MediaKind, Form(description="photo, video, video_note или voice")],
    file: Annotated[UploadFile | None, File(description="Байты фото, видео, кружка, аудио или голосового")] = None,
    tg_file_id: Annotated[
        str | None,
        Form(description="file_id Telegram. Собеседнику в Telegram уходит исходный файл"),
    ] = None,
    tg_method: Annotated[
        TgSend | None,
        Form(description="photo, audio, voice, video или video_note"),
    ] = None,
) -> MediaOut:
    """Кладёт файл в S3 и ставит событие `media` собеседнику.

    Фото, аудио, голос, видео и кружок уходят, только если премиум есть и у отправителя,
    и у собеседника. Иначе файл не читается и не сохраняется, ответ 403 с «премиум» в detail:
    «Фото, видео и аудио можно отправлять только с премиумом» — нет премиума у отправителя,
    «Собеседник без премиума не может получать медиа» — у живого собеседника.
    Собеседнику из chat-engine премиум не нужен.
    Вид проверяется по байтам файла, а не по имени.
    Байты забирает бот собеседника отдельным запросом.
    Если байтов нет, но есть file_id Telegram, событие уходит только собеседнику в Telegram:
    бот не скачивает файл больше 20 МБ, а кружок и голосовое отправляются как есть.
    """
    if (await chat.state(peer)).phase != "chatting":
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_CHATTING)
    blocked = await chat.media_block_reason(peer)
    if blocked is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, blocked)
    file_id, method = _telegram_ref(peer.platform, kind, tg_file_id, tg_method)
    raw = await file.read(MAX_BYTES[kind] + 1) if file is not None else b""
    if raw:
        try:
            saved = await media.save(peer, kind, raw)
        except StorageDisabled as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Хранилище медиа не настроено") from exc
        except MediaRejected as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except StorageError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось сохранить файл") from exc
        await chat.forward_media(
            peer,
            saved.id,
            saved.kind,
            saved.content_type,
            tg_file_id=file_id,
            tg_method=method,
        )
        return saved
    if file_id is None or method is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    result = await chat.forward_media(
        peer,
        None,
        kind,
        None,
        tg_file_id=file_id,
        tg_method=method,
    )
    if result == "left":
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_CHATTING)
    if result != "sent":
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Собеседник этот файл не получит")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/premium/link",
    summary="Ссылка на оплату премиума",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "PREMIUM_LINK_SECRET не задан"}},
)
async def premium_pay_link(peer: HumanPeer, chat: Chat, settings: AppSettings) -> PremiumLinkOut:
    """Адрес страницы оплаты для этого пользователя и текущий срок премиума.

    Пользователь передаётся в пути `/checkout/<токен>` подписанным и зашифрованным токеном (24 часа),
    открытого id в адресе нет. Страница оплаты по нему входит без email.
    Покупка привязана к аккаунту платформы, а не к боту: `bot_key` в токен не входит.
    """
    if not settings.premium_link_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Оплата премиума не настроена")
    token = premium_link.sign(peer.platform, peer.user_id, settings.premium_link_secret)
    query = urlencode({"utm_source": peer.platform, "utm_campaign": "premium"})
    until = await chat.premium_until(peer)
    return PremiumLinkOut(
        url=f"{settings.pay_public_url}/checkout/{token}?{query}",
        premium_active=bool(until and until > datetime.now(timezone.utc)),
        premium_until=until,
    )


def _telegram_ref(
    platform: str,
    kind: MediaKind,
    file_id: str | None,
    method: TgSend | None,
) -> tuple[str | None, TgSend | None]:
    """file_id принадлежит боту Telegram и на другие платформы не переносится."""
    if platform != "tg":
        return None, None
    cleaned = (file_id or "").strip()
    if not cleaned or len(cleaned) > 512:
        return None, None
    if kind == "photo":
        return cleaned, "photo"
    if kind == "video_note":
        return cleaned, "video_note"
    if kind == "video":
        return cleaned, "video"
    if method == "audio":
        return cleaned, "audio"
    return cleaned, "voice"


@router.post("/delivery-failed", status_code=status.HTTP_204_NO_CONTENT, summary="Сообщить о недоставке")
async def delivery_failed(peer: ServicePeer, chat: Chat) -> None:
    """Клиент не смог доставить событие этому пользователю, например он заблокировал бота.

    Поиск или диалог пользователя закрывается, собеседник получает
    `partner_left` с причиной `undeliverable`.
    """
    await chat.delivery_failed(peer)
