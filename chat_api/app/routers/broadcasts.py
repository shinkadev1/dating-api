"""Рассылки из CRM: по времени и через паузу после события.

Отправляет фоновая задача (app/broadcasts/service.py). Маршруты только хранят и меняют рассылки.
Доступ — токен CRM (CHAT_API_CRM_TOKEN) или сервисный токен бота.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status

from app.deps import CurrentClient, MediaSvc
from app.media_types import MAX_BYTES, MediaRejected
from app.peer import Peer
from app.repositories import broadcasts as repo
from app.schemas import AudienceOut, BroadcastIn, BroadcastOut, BroadcastPatch, BroadcastPlatform
from app.services.media import StorageDisabled
from app.services.storage import StorageError

_IMAGE_PEER = Peer("web", "broadcast", 0)


def require_crm(client: CurrentClient) -> None:
    if client not in ("crm", "bot"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужен токен CRM или сервисный токен бота")


router = APIRouter(
    prefix="/broadcasts",
    tags=["broadcasts"],
    dependencies=[Depends(require_crm)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токен не подходит для рассылок"},
    },
)


def _pool(request: Request):
    return request.app.state.pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _out(row: dict[str, Any]) -> BroadcastOut:
    return BroadcastOut(**{k: v for k, v in row.items() if k not in ("media_id", "content_type")}, has_image=bool(row["media_id"]))


async def _get_or_404(conn, broadcast_id: int) -> dict[str, Any]:
    row = await repo.get(conn, broadcast_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Рассылки нет")
    return row


@router.get("", summary="Все рассылки")
async def list_broadcasts(request: Request) -> list[BroadcastOut]:
    async with _pool(request).connection() as conn:
        return [_out(r) for r in await repo.list_all(conn)]


@router.get("/audience", summary="Сколько получателей у рассылки по времени")
async def audience(
    request: Request,
    platforms: Annotated[list[BroadcastPlatform], Query(min_length=1)],
) -> AudienceOut:
    async with _pool(request).connection() as conn:
        return AudienceOut(recipients=await repo.audience(conn, list(platforms)))


@router.post("", status_code=status.HTTP_201_CREATED, summary="Создать рассылку")
async def create_broadcast(body: BroadcastIn, request: Request) -> BroadcastOut:
    values: dict[str, Any] = {
        "title": body.title.strip(),
        "text": body.text,
        "platforms": sorted(set(body.platforms)),
        "kind": body.kind,
        "send_at": None,
        "event": None,
        "delay_minutes": None,
        "repeat": False,
        "active_since": None,
    }
    if body.kind == "at":
        values.update(send_at=body.send_at or _now(), status="scheduled")
    else:
        if body.event is None or body.delay_minutes is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Для рассылки после события нужны event и delay_minutes")
        values.update(
            event=body.event, delay_minutes=body.delay_minutes, repeat=body.repeat,
            status="active", active_since=_now(),
        )
    async with _pool(request).connection() as conn:
        return _out(await repo.create(conn, values))


@router.get("/{broadcast_id}", summary="Одна рассылка")
async def get_broadcast(broadcast_id: int, request: Request) -> BroadcastOut:
    async with _pool(request).connection() as conn:
        return _out(await _get_or_404(conn, broadcast_id))


@router.patch("/{broadcast_id}", summary="Изменить, поставить на паузу или отменить")
async def patch_broadcast(broadcast_id: int, body: BroadcastPatch, request: Request) -> BroadcastOut:
    patch = body.model_dump(exclude_unset=True)
    async with _pool(request).connection() as conn:
        row = await _get_or_404(conn, broadcast_id)
        values: dict[str, Any] = {}
        finished = row["status"] in ("done", "cancelled")
        new_status = patch.pop("status", None)
        if finished and (patch or new_status not in (None, row["status"])):
            raise HTTPException(status.HTTP_409_CONFLICT, "Рассылка уже завершена или отменена")
        if row["kind"] == "at":
            if row["status"] == "sending" and patch:
                raise HTTPException(status.HTTP_409_CONFLICT, "Рассылка уже отправляется; её можно только отменить")
            for key in ("delay_minutes", "repeat"):
                if patch.pop(key, None) is not None:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key} — только для рассылки после события")
            if new_status not in (None, "cancelled", row["status"]):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Рассылку по времени можно только отменить")
        else:
            if patch.pop("send_at", None) is not None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "send_at — только для рассылки по времени")
            if new_status == "scheduled":
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Для рассылки после события: active, paused или cancelled")
        if "platforms" in patch:
            patch["platforms"] = sorted(set(patch["platforms"]))
        if "title" in patch:
            patch["title"] = (patch["title"] or "").strip()
        values.update({k: v for k, v in patch.items() if v is not None or k == "title"})
        if new_status and new_status != row["status"]:
            values["status"] = new_status
            if new_status == "cancelled":
                values["finished_at"] = _now()
        return _out(await repo.update(conn, broadcast_id, values))


@router.delete("/{broadcast_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить рассылку")
async def delete_broadcast(broadcast_id: int, request: Request) -> None:
    async with _pool(request).connection() as conn:
        row = await _get_or_404(conn, broadcast_id)
        if row["status"] == "sending":
            raise HTTPException(status.HTTP_409_CONFLICT, "Рассылка отправляется: сначала отмените её")
        await repo.delete(conn, broadcast_id)


@router.put("/{broadcast_id}/image", summary="Фото к рассылке")
async def put_image(
    broadcast_id: int, request: Request, media: MediaSvc, file: Annotated[UploadFile, File()]
) -> BroadcastOut:
    data = await file.read(MAX_BYTES["photo"] + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пустой файл")
    async with _pool(request).connection() as conn:
        row = await _get_or_404(conn, broadcast_id)
        if row["status"] in ("sending", "done", "cancelled"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Фото можно менять только до отправки")
        try:
            saved = await media.save(_IMAGE_PEER, "photo", data)
        except StorageDisabled as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Хранилище медиа не настроено") from exc
        except MediaRejected as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except StorageError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось сохранить фото") from exc
        return _out(await repo.update(conn, broadcast_id, {"media_id": str(saved.id), "content_type": saved.content_type}))


@router.delete("/{broadcast_id}/image", summary="Убрать фото")
async def delete_image(broadcast_id: int, request: Request) -> BroadcastOut:
    async with _pool(request).connection() as conn:
        row = await _get_or_404(conn, broadcast_id)
        if row["status"] in ("sending", "done", "cancelled"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Фото можно менять только до отправки")
        return _out(await repo.update(conn, broadcast_id, {"media_id": None, "content_type": None}))
