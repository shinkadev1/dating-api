"""Продуктовая аналитика для Bot CRM: люди, новые, активность, retention, воронка. Дни — по Москве.

Доступ — токен CRM (CHAT_API_CRM_TOKEN) или сервисный токен бота, как у рассылок.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from app.repositories import analytics as repo
from app.routers.broadcasts import require_crm
from app.schemas import BroadcastPlatform

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
