"""События для второй стороны диалога через Redis Streams.

Поток на платформу: `chat:events:vk`, `chat:events:tg`, `chat:events:web`, `chat:events:engine`.
Боты и engine_bridge читают свои потоки группой потребителей: напрямую из Redis или,
с другого сервера, через WebSocket `/events/{platform}`. Поток сайта читает сам API
и отдаёт серверному приложению по WebSocket.
Уведомления всем диалогам идут отдельно: `chat:broadcast:vk`, `chat:broadcast:tg`,
`chat:broadcast:web`. Так рассылка не вытесняет живые реплики из короткого потока диалога.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.peer import ENGINE_PLATFORM, Peer
from app.schemas import ChatEvent

logger = logging.getLogger(__name__)

STREAM_PREFIX = "chat:events:"
BROADCAST_PREFIX = "chat:broadcast:"
BOTS_GROUP = "bots"
_BLOCK_MS = 5000
# Рассылка может быть длиннее живого потока: его maxlen не должен срезать уведомления.
_BROADCAST_MAXLEN = 1_000_000
# Таймаут сокета должен быть длиннее блокирующего XREAD, иначе чтение обрывается.
REDIS_SOCKET_TIMEOUT = _BLOCK_MS / 1000 + 10


def stream_name(platform: str) -> str:
    return f"{STREAM_PREFIX}{platform}"


def broadcast_stream(platform: str) -> str:
    return f"{BROADCAST_PREFIX}{platform}"


class EventBus:
    """Публикует события в поток платформы получателя."""

    def __init__(self, redis: Redis, maxlen: int) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def publish(self, recipient: Peer, event: ChatEvent) -> None:
        body = {"to": recipient.as_dict(), **event.model_dump(exclude_none=True)}
        await self._add(stream_name(recipient.platform), body, f"{event.type} для {recipient}")

    async def publish_notices(self, recipients: list[Peer], text: str) -> None:
        """Кладёт уведомление в поток рассылки платформы. Пустой список ничего не пишет."""
        if not recipients:
            return
        event = ChatEvent(type="notice", text=text).model_dump(exclude_none=True)
        sent = 0
        try:
            for recipient in recipients:
                body = {"to": recipient.as_dict(), **event}
                await self._redis.xadd(
                    broadcast_stream(recipient.platform),
                    {"data": json.dumps(body, ensure_ascii=False)},
                    maxlen=_BROADCAST_MAXLEN,
                    approximate=True,
                )
                sent += 1
        except Exception:
            logger.exception("Уведомление оборвалось на %s из %s", sent, len(recipients))
            raise
        logger.info("Уведомление поставлено в очередь для %s диалогов", sent)

    async def request_engine(self) -> None:
        """Человек остался в очереди один: engine_bridge сразу запускает поиск в chat-engine."""
        await self._add(stream_name(ENGINE_PLATFORM), {"type": "demand"}, "demand")

    async def _add(self, stream: str, body: dict, label: str) -> None:
        try:
            await self._redis.xadd(
                stream,
                {"data": json.dumps(body, ensure_ascii=False)},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:
            logger.exception("Не опубликовал событие %s", label)


class BotEventFeed:
    """Поток событий бота по WebSocket от имени потребителя группы `bots`.

    Сервер шлёт `{"id", "event"}`, клиент отвечает `{"ack": id}` после доставки.
    Неподтверждённые события приходят снова при следующем подключении того же потребителя.
    """

    def __init__(
        self,
        redis: Redis,
        platform: str,
        consumer: str,
        *,
        stream: str | None = None,
        start: str = "$",
    ) -> None:
        self._redis = redis
        self._stream = stream or stream_name(platform)
        self._consumer = consumer
        self._start = start

    async def serve(self, websocket: WebSocket) -> None:
        try:
            await self._redis.xgroup_create(self._stream, BOTS_GROUP, id=self._start, mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        sender = asyncio.create_task(self._send(websocket))
        try:
            while True:
                message = await websocket.receive_json()
                entry_id = message.get("ack") if isinstance(message, dict) else None
                if isinstance(entry_id, str):
                    await self._redis.xack(self._stream, BOTS_GROUP, entry_id)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    async def _send(self, websocket: WebSocket) -> None:
        """Сначала неподтверждённые события этого потребителя, потом новые."""
        cursor = "0"
        while True:
            try:
                batches = await self._redis.xreadgroup(
                    BOTS_GROUP, self._consumer, {self._stream: cursor}, count=50, block=_BLOCK_MS
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не прочитал %s, повтор через секунду", self._stream)
                await asyncio.sleep(1)
                continue
            for _stream, entries in batches or []:
                if not entries and cursor != ">":
                    cursor = ">"
                for entry_id, fields in entries:
                    if cursor != ">":
                        cursor = entry_id
                    try:
                        event = json.loads(fields["data"])
                    except (KeyError, ValueError):
                        logger.warning("Битое событие в %s: %r", self._stream, fields)
                        await self._redis.xack(self._stream, BOTS_GROUP, entry_id)
                        continue
                    await websocket.send_json({"id": entry_id, "event": event})


class WebSocketHub:
    """Держит WebSocket-соединения приложения сайта с этой копией API и отдаёт им события web."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._sockets: set[WebSocket] = set()

    def add(self, socket: WebSocket) -> None:
        self._sockets.add(socket)

    def remove(self, socket: WebSocket) -> None:
        self._sockets.discard(socket)

    async def _tail(self, stream: str) -> str:
        """Последний id потока. Реальный id не пропускает события, пришедшие между чтениями."""
        rows = await self._redis.xrevrange(stream, count=1)
        return rows[0][0] if rows else "0-0"

    async def run(self) -> None:
        """Читает потоки сайта с текущего конца. Каждая копия API видит все события."""
        cursors: dict[str, str] = {}
        for stream in (stream_name("web"), broadcast_stream("web")):
            while stream not in cursors:
                try:
                    cursors[stream] = await self._tail(stream)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Не прочитал конец %s, повтор через секунду", stream)
                    await asyncio.sleep(1)
        while True:
            try:
                batches = await self._redis.xread(cursors, block=_BLOCK_MS, count=100)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не прочитал поток сайта, повтор через секунду")
                await asyncio.sleep(1)
                continue
            for name, entries in batches or []:
                for entry_id, fields in entries:
                    cursors[name] = entry_id
                    await self._dispatch(fields)

    async def _dispatch(self, fields: dict) -> None:
        raw = fields.get("data")
        if raw is None:
            return
        try:
            body = json.loads(raw)
        except ValueError:
            logger.warning("Битое событие в потоке сайта: %r", raw)
            return
        for socket in list(self._sockets):
            try:
                await socket.send_json(body)
            except Exception:  # noqa: BLE001 — закрытое соединение снимется в обработчике WebSocket
                self.remove(socket)
