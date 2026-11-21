"""Продуктовая аналитика для Bot CRM: люди, новые, активность, retention, воронка. Дни — по Москве.

Доступ — токен CRM (CHAT_API_CRM_TOKEN) или сервисный токен бота, как у рассылок.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.repositories import analytics as repo
from app.repositories import incidents as incidents_repo
from app.routers.broadcasts import require_crm
from app.schemas import BroadcastPlatform, ChatMode
from app.services.incidents import REASONS

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(require_crm)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Нет токена или он не подходит"},
        status.HTTP_403_FORBIDDEN: {"description": "Токен не подходит"},
    },
)


class AnalyticsOut(BaseModel):
    today: date = Field(description="Сегодня по Москве")
    summary: dict[str, Any] = Field(
        description="users, new_1/7/30 и *_prev за прошлый такой же период, premium_active, premium_ever, "
        "dau, wau, mau, activity_since — с какого дня есть журнал активности"
    )
    daily: list[dict[str, Any]] = Field(description="По дням: day, new, active, dialogs")
    retention: dict[str, Any] = Field(description="d1/d7/d30: base — сколько людей учтено, kept — сколько вернулись")
    cohorts: list[dict[str, Any]] = Field(description="Недельные когорты: week, size, kept[неделя 0..N]")
    funnel: list[dict[str, Any]] = Field(description="Воронка: step, label, users")
    engagement: dict[str, Any] = Field(description="dialogs_30, median_minutes, reported_30, searches_per_user")


class IncidentsOut(BaseModel):
    items: list[dict[str, Any]] = Field(
        description="Инциденты, новые первыми: id, created_at, platform, bot_key, user_id, mode, searching_since, "
        "ended_at, wait_seconds, outcome (stopped/mode_changed), reason, snapshot"
    )
    summary: dict[str, int] = Field(description="Сколько инцидентов по каждой причине за период (все страницы)")
    total: int = Field(description="Сумма summary: всего инцидентов за период с этими фильтрами")
    limit: int
    offset: int


def _utc(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("/incidents", summary="Журнал инцидентов поиска")
async def incidents(
    request: Request,
    date_from: Annotated[datetime | None, Query(description="С этого момента включительно (ISO, без пояса — UTC)")] = None,
    date_to: Annotated[datetime | None, Query(description="До этого момента, не включая (ISO, без пояса — UTC)")] = None,
    mode: ChatMode | None = None,
    reason: Annotated[str | None, Query(description=", ".join(REASONS))] = None,
    platform: BroadcastPlatform | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentsOut:
    """Человек ждал собеседника и вышел без пары: стоп или смена режима, с причиной и снимком очереди."""
    if reason is not None and reason not in REASONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"reason: одно из {', '.join(REASONS)}")
    async with request.app.state.pool.connection() as conn:
        items, summary = await incidents_repo.list_incidents(
            conn,
            date_from=_utc(date_from),
            date_to=_utc(date_to),
            mode=mode,
            reason=reason,
            platform=platform,
            limit=limit,
            offset=offset,
        )
    return IncidentsOut(items=items, summary=summary, total=sum(summary.values()), limit=limit, offset=offset)


@router.get("", summary="Вся аналитика одной выборкой")
async def analytics(
    request: Request,
    platforms: Annotated[list[BroadcastPlatform], Query(min_length=1, description="vk, tg, web")],
    days: Annotated[int, Query(ge=7, le=180, description="Сколько дней в графиках")] = 30,
) -> AnalyticsOut:
    p = sorted(set(platforms))
    async with request.app.state.pool.connection() as conn:
        day = await repo.today(conn)
        return AnalyticsOut(
            today=day,
            summary=await repo.summary(conn, p, day),
            daily=await repo.daily(conn, p, day, days),
            retention=await repo.day_retention(conn, p, day),
            cohorts=await repo.cohorts(conn, p, day),
            funnel=await repo.funnel(conn, p),
            engagement=await repo.engagement(conn, p),
        )
