"""Пользователи ботов для Bot CRM: только чтение, без переписки.

Доступ — токен CRM (CHAT_API_CRM_TOKEN) или сервисный токен бота, как у рассылок.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.repositories import users as repo
from app.routers.broadcasts import require_crm
from app.schemas import BroadcastPlatform, UserDetailOut, UserListOut, UserRowOut, UserStatsOut

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_crm)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токен не подходит"},
    },
)

Platforms = Annotated[list[BroadcastPlatform], Query(min_length=1, description="vk, tg, web")]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/stats", summary="Сводка по людям бота")
async def user_stats(request: Request, platforms: Platforms) -> UserStatsOut:
    async with request.app.state.pool.connection() as conn:
        return UserStatsOut(**await repo.stats(conn, list(platforms), _now()))


@router.get("", summary="Список людей с поиском и фильтрами")
async def list_users(
    request: Request,
    platforms: Platforms,
    q: Annotated[str, Query(max_length=100, description="ID или имя пользователя (handle)")] = "",
    premium: Literal["any", "yes", "no", "ended"] = "any",
    active: Literal["any", "24h", "7d", "30d", "gone"] = "any",
    sort: Literal["last_seen", "started", "searches", "premium"] = "last_seen",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListOut:
    async with request.app.state.pool.connection() as conn:
        total, rows = await repo.search(
            conn, platforms=list(platforms), q=q, premium=premium, active=active,
            sort=sort, limit=limit, offset=offset, now=_now(),
        )
    return UserListOut(total=total, items=[UserRowOut(**r) for r in rows])


@router.get("/{platform}/{user_id}", summary="Карточка человека")
async def user_detail(platform: BroadcastPlatform, user_id: int, request: Request) -> UserDetailOut:
    async with request.app.state.pool.connection() as conn:
        user = await repo.detail(conn, platform, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого человека нет")
    return UserDetailOut(**user, channels=len(user["chats"]))
