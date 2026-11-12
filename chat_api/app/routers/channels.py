"""Чаты, куда подключён бот: сообщества ВК, бот Telegram, сайт."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app import channel_tokens, vk_groups
from app.deps import CLIENT_PLATFORMS, Chat, CurrentClient, MediaSvc
from app.media_types import MAX_BYTES, MediaRejected
from app.peer import Peer
from app.repositories import channels as repo
from app.schemas import (
    ChannelIn,
    NoticeIn,
    NoticeOut,
    VkCheckOut,
    VkCommunityOut,
    VkEnabledIn,
    VkImportIn,
    VkImportOut,
    VkStatusIn,
    VkTokenIn,
    VkTokenItem,
    VkTokensOut,
)
from app.services.media import StorageDisabled
from app.services.storage import StorageError

logger = logging.getLogger(__name__)

# Одно фото на всю рассылку. К диалогу человека оно не привязано.
_BROADCAST_PEER = Peer("web", "broadcast", 0)

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


@router.post(
    "/notifications",
    summary="Уведомить все чаты бота",
    responses={
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {"description": "Фото больше 20 МБ"},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Файл не является изображением"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Нет текста"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Бакет не принял фото"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "S3 не настроен"},
    },
)
async def notify_channels(request: Request, client: CurrentClient, chat: Chat, media: MediaSvc) -> NoticeOut:
    """Ставит текст в очередь доставки по всем чатам, куда подключён бот.

    JSON `{"text": "..."}` или форма: поле `text` и необязательный файл `file`.
    Файл — одно фото на всю рассылку, оно уходит вместе с текстом.
    Чат — сообщество ВК, бот Telegram или сайт. Сообщение уходит каждому человеку,
    который уже писал боту в этом чате. Если человек открыл два сообщества,
    он получит текст в обоих: из чужого сообщества ВК написать нельзя.
    Нужен сервисный токен бота. Текущий анонимный диалог не закрывается,
    а доставка идёт отдельно от живых реплик.
    """
    if client != "bot":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен сервисный токен бота")
    text, image = await _read_notice(request)
    media_id = None
    content_type = None
    if image:
        try:
            saved = await media.save(_BROADCAST_PEER, "photo", image)
        except StorageDisabled as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Хранилище медиа не настроено") from exc
        except MediaRejected as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except StorageError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось сохранить фото") from exc
        media_id = str(saved.id)
        content_type = saved.content_type
    return NoticeOut(
        queued=await chat.notify_channels(text, media_id=media_id, content_type=content_type)
    )


async def _read_notice(request: Request) -> tuple[str, bytes | None]:
    header = request.headers.get("content-type", "")
    if "multipart/form-data" in header.lower():
        form = await request.form()
        text = str(form.get("text") or "").strip()
        upload = form.get("file")
        image = None
        if isinstance(upload, UploadFile):
            image = await upload.read(MAX_BYTES["photo"] + 1)
            if not image:
                image = None
        _require_text(text)
        return text, image
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нужен текст уведомления") from exc
    try:
        text = NoticeIn.model_validate(payload).text.strip()
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нужен текст уведомления") from exc
    _require_text(text)
    return text, None


def _require_text(text: str) -> None:
    if not text or len(text) > 4096:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нужен текст уведомления до 4096 символов")


# ---------- сообщества ВК, которыми управляет CRM ----------
# Полный ключ получает только бот (GET /channels/vk/tokens); CRM видит подсказку token_hint.


def _only(client: str, expected: str, who: str) -> None:
    if client != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Нужен токен {who}")


def _token(value: str) -> str:
    """Длина ключа. Текст ошибки не содержит значения: ответ видит CRM."""
    if not value or len(value) > 512:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Ключ пустой или длиннее 512 знаков")
    return value


def _pool(request: Request):
    return request.app.state.pool


def _community(row: dict[str, Any]) -> VkCommunityOut:
    if not row["enabled"]:
        state = "disabled"
    elif row["status"] in ("ok", "error"):
        state = row["status"]
    else:
        state = "unknown"
    return VkCommunityOut(
        group_id=int(row["group_id"]),
        title=row["title"],
        photo_url=row["photo_url"],
        token_hint=row["token_hint"],
        has_token=bool(row["has_token"]),
        enabled=bool(row["enabled"]),
        status=state,
        error=row["status_error"],
        last_seen_at=row["last_seen_at"],
    )


async def _check(token: str) -> vk_groups.GroupInfo:
    try:
        return await vk_groups.check_token(token)
    except vk_groups.VkRejected as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Ключ не подошёл: {exc.msg} (код {exc.code})"
        ) from None
    except vk_groups.VkUnavailable as exc:
        logger.warning("ВК не ответил при проверке ключа: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "ВК недоступен, попробуйте позже") from None


_VK_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Ключ пустой, длиннее 512 знаков или ВК его отклонил"},
    status.HTTP_502_BAD_GATEWAY: {"description": "ВК недоступен"},
}


@router.get("/channels/vk", summary="Сообщества ВК")
async def list_vk(request: Request, client: CurrentClient) -> list[VkCommunityOut]:
    """Подключённые сообщества: название, подсказка ключа, включено ли, статус бота. Токен CRM или бота."""
    if client not in ("crm", "bot"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен токен CRM или сервисный токен бота")
    async with _pool(request).connection() as conn:
        return [_community(row) for row in await repo.list_vk(conn)]


@router.post("/channels/vk/check", summary="Проверить ключ сообщества", responses=_VK_RESPONSES)
async def check_vk(body: VkTokenIn, request: Request, client: CurrentClient) -> VkCheckOut:
    """Спрашивает ВК, чей это ключ и включён ли Long Poll. Ничего не записывает."""
    _only(client, "crm", "CRM")
    info = await _check(_token(body.token))
    async with _pool(request).connection() as conn:
        known = await repo.get_vk(conn, info.group_id)
    return VkCheckOut(
        group_id=info.group_id,
        title=info.title,
        photo_url=info.photo_url,
        long_poll=info.long_poll,
        connected=known is not None,
        enabled=bool(known["enabled"]) if known else None,
    )


@router.post(
    "/channels/vk",
    status_code=status.HTTP_201_CREATED,
    summary="Подключить сообщество",
    responses=_VK_RESPONSES,
)
async def connect_vk(body: VkTokenIn, request: Request, client: CurrentClient) -> VkCommunityOut:
    """Проверяет ключ и сохраняет. Сообщество включается; бот подхватит его в течение минуты."""
    _only(client, "crm", "CRM")
    info = await _check(_token(body.token))
    async with _pool(request).connection() as conn:
        row = await repo.connect_vk(
            conn,
            info.group_id,
            title=info.title,
            photo_url=info.photo_url,
            token=body.token,
            token_hint=channel_tokens.hint(body.token),
        )
    return _community(row)


@router.get("/channels/vk/tokens", summary="Ключи включённых сообществ")
async def vk_tokens(request: Request, client: CurrentClient) -> VkTokensOut:
    """Для бота ВК: включённые сообщества с ключами и id отключённых. Только сервисный токен бота."""
    _only(client, "bot", "бота")
    async with _pool(request).connection() as conn:
        rows = await repo.enabled_tokens(conn)
        disabled = await repo.disabled_ids(conn)
    communities = [VkTokenItem(group_id=int(r["group_id"]), title=r["title"], token=r["token"]) for r in rows]
    return VkTokensOut(communities=communities, disabled=disabled)


@router.post("/channels/vk/import", summary="Сохранить ключ из окружения бота")
async def import_vk(body: VkImportIn, request: Request, client: CurrentClient) -> VkImportOut:
    """Бот переносит в CRM ключи из VK_TOKEN(S). Существующий ключ и переключатель не меняются."""
    _only(client, "bot", "бота")
    _token(body.token)
    async with _pool(request).connection() as conn:
        stored = await repo.import_token(
            conn,
            body.group_id,
            title=body.title.strip(),
            token=body.token,
            token_hint=channel_tokens.hint(body.token),
        )
    return VkImportOut(stored=stored)


@router.post("/channels/vk/status", status_code=status.HTTP_204_NO_CONTENT, summary="Статус сообществ от бота")
async def vk_status(body: VkStatusIn, request: Request, client: CurrentClient) -> None:
    """Бот сообщает, работает ли сообщество. Неизвестные сообщества игнорируются."""
    _only(client, "bot", "бота")
    items = [(item.group_id, item.status, item.error) for item in body.items]
    async with _pool(request).connection() as conn:
        await repo.report_status(conn, items)


@router.put(
    "/channels/vk/{group_id}/token",
    summary="Заменить ключ сообщества",
    responses={
        **_VK_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "Сообщества нет"},
        status.HTTP_409_CONFLICT: {"description": "Ключ от другого сообщества"},
    },
)
async def replace_vk_token(group_id: int, body: VkTokenIn, request: Request, client: CurrentClient) -> VkCommunityOut:
    _only(client, "crm", "CRM")
    _token(body.token)
    async with _pool(request).connection() as conn:
        if await repo.get_vk(conn, group_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщества нет")
    info = await _check(_token(body.token))
    if info.group_id != group_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ключ от другого сообщества")
    async with _pool(request).connection() as conn:
        row = await repo.replace_token(
            conn,
            group_id,
            title=info.title,
            photo_url=info.photo_url,
            token=body.token,
            token_hint=channel_tokens.hint(body.token),
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщества нет")
    return _community(row)


@router.patch(
    "/channels/vk/{group_id}",
    summary="Включить или отключить сообщество",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Сообщества нет"}},
)
async def toggle_vk(group_id: int, body: VkEnabledIn, request: Request, client: CurrentClient) -> VkCommunityOut:
    """Отключённое сообщество не получает рассылки и уведомления, бот его останавливает. Данные остаются."""
    _only(client, "crm", "CRM")
    async with _pool(request).connection() as conn:
        row = await repo.set_enabled(conn, group_id, body.enabled)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщества нет")
    return _community(row)
