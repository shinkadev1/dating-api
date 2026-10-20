"""FastAPI-приложение Chat API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import get_settings
from app.db import create_pool, ensure_schema
from app.events import REDIS_SOCKET_TIMEOUT, EventBus, WebSocketHub
from app.routers import channels, engine, media, peers, stats, visitors, ws
from app.services.chat import ChatService
from app.services.media import MediaService
from app.services.storage import ObjectStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pool = create_pool(settings.database_url)
    await pool.open(wait=True, timeout=60)
    await ensure_schema(pool)
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=5,
    )
    hub = WebSocketHub(redis)
    hub_task = asyncio.create_task(hub.run(), name="web-events")

    storage = ObjectStorage(
        endpoint=settings.s3_endpoint_url,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    await storage.open()
    app.state.chat = ChatService(pool, EventBus(redis, settings.events_maxlen), settings)
    app.state.media = MediaService(pool, storage)
    app.state.hub = hub
    app.state.pool = pool
    app.state.redis = redis
    try:
        yield
    finally:
        hub_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hub_task
        await storage.close()
        await redis.aclose()
        await pool.close()


app = FastAPI(title="Chat API", version="1.3.0", lifespan=lifespan)

app.include_router(peers.router, prefix="/api/v1")
app.include_router(media.router, prefix="/api/v1")
app.include_router(visitors.router, prefix="/api/v1")
app.include_router(channels.router, prefix="/api/v1")
app.include_router(engine.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")
app.include_router(ws.router, prefix="/api/v1")


@app.get("/health", tags=["service"], summary="Проверка готовности")
async def health() -> dict[str, str]:
    """Проверяет базу и Redis: бот ждёт этот ответ перед запуском."""
    async with app.state.pool.connection() as conn:
        await conn.execute("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}
