"""Чтение сохранённого файла. Ссылка в событии не публичная: нужен токен бота, сайта или engine_bridge."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.deps import CurrentClient, MediaSvc
from app.services.media import StorageDisabled
from app.services.storage import StorageError

router = APIRouter(prefix="/media", tags=["media"])


@router.get(
    "/{media_id}",
    summary="Скачать медиа",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Записи нет"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Бакет не отдал файл"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "S3 не настроен"},
    },
)
async def download_media(media_id: UUID, client: CurrentClient, media: MediaSvc) -> Response:
    """Отдаёт байты файла, который собеседник получил в событии `media`.

    Токен бота, сайта или engine_bridge. Идентификатор знает только сторона, которой событие адресовано.
    """
    try:
        loaded = await media.load(media_id)
    except StorageDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Хранилище медиа не настроено") from exc
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось прочитать файл") from exc
    if loaded is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    kind, content_type, body = loaded
    return Response(
        content=body,
        media_type=content_type,
        headers={"X-Media-Kind": kind, "Cache-Control": "private, max-age=3600"},
    )
