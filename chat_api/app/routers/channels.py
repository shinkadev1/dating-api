"""Чаты, куда подключён бот: сообщества ВК, бот Telegram, сайт."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.deps import CLIENT_PLATFORMS, Chat, CurrentClient, MediaSvc
from app.media_types import MAX_BYTES, MediaRejected
from app.peer import Peer
from app.schemas import ChannelIn, NoticeIn, NoticeOut
from app.services.media import StorageDisabled
from app.services.storage import StorageError

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
