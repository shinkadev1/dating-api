"""Логика анонимного чата: очередь поиска, пары и пересылка реплик."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.events import EventBus
from app.peer import Peer
from app.repositories import sessions as repo
from app.repositories.visitors import save_web_visitor
from app.schemas import (
    ChatEvent,
    EngineAttachOut,
    EngineDemandOut,
    LeftReason,
    MessageOut,
    Phase,
    PresenceOut,
    StateOut,
    StatsOut,
    StopOut,
    VisitorOut,
)

Outbox = list[tuple[Peer, ChatEvent]]

# Счётчики для сайта не обязаны быть точными до секунды, а запрос считает всю таблицу.
_STATS_TTL_SECONDS = 5.0


class NotChattingError(Exception):
    """Действие доступно только в активном диалоге."""


class ChatService:
    """Все изменения пар идут под одной блокировкой, события уходят после коммита."""

    def __init__(self, pool: AsyncConnectionPool, events: EventBus, settings: Settings) -> None:
        self._pool = pool
        self._events = events
        self._engine_enabled = settings.engine_enabled
        self._fallback_after = settings.engine_fallback_after_seconds
        self._online_window = settings.stats_online_window_seconds
        self._stats: tuple[float, StatsOut] | None = None

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as conn, conn.transaction():
            await repo.lock_matchmaking(conn)
            yield conn

    async def _flush(self, outbox: Outbox) -> None:
        for recipient, event in outbox:
            await self._events.publish(recipient, event)

    @staticmethod
    async def _linked_partner(conn: AsyncConnection, peer: Peer, session: repo.SessionRow | None) -> Peer | None:
        """Собеседник, если диалог связан с двух сторон."""
        if session is None or session.phase != "chatting" or session.partner is None:
            return None
        other = await repo.get_session(conn, session.partner)
        if other is None or other.phase != "chatting" or other.partner != peer:
            return None
        return session.partner

    async def _notify_left(
        self,
        conn: AsyncConnection,
        other: Peer,
        reason: LeftReason,
        outbox: Outbox,
        *,
        skip: Peer,
    ) -> None:
        """Снимает собеседника и сообщает ему об уходе. Если у него ещё висит chat-engine — закрывает и его."""
        session = await repo.get_session(conn, other)
        extra = (
            session.partner
            if session is not None and session.partner is not None and session.partner != skip
            else None
        )
        await repo.set_idle(conn, other)
        outbox.append((other, ChatEvent(type="partner_left", reason=reason)))
        if extra is not None:
            await repo.set_idle(conn, extra)
            outbox.append((extra, ChatEvent(type="partner_left", reason=reason)))

    async def _detach(self, conn: AsyncConnection, peer: Peer, reason: LeftReason, outbox: Outbox) -> bool:
        """Снимает пользователя с поиска или диалога. True, если было что снимать.

        Сообщает собеседнику даже если связь в сессиях уже несимметрична: иначе
        человек, которому нашли пару, не узнает, что второй нажал «Следующий».
        """
        session = await repo.get_session(conn, peer)
        if session is None or session.phase == "idle":
            return False
        partner = session.partner if session.phase == "chatting" else None
        await repo.set_idle(conn, peer)
        if partner is not None:
            await self._notify_left(conn, partner, reason, outbox, skip=peer)
        return True

    async def _search(self, peer: Peer, left_reason: LeftReason) -> StateOut:
        outbox: Outbox = []
        async with self._locked() as conn:
            await self._detach(conn, peer, left_reason, outbox)
            await repo.set_searching(conn, peer)
            await repo.record_search(conn, peer)
            phase: Phase = "searching"
            other = await repo.find_searcher(conn, peer)
            stolen = False
            if other is None and not peer.is_engine:
                paired = await repo.find_human_with_engine(conn)
                if paired is not None:
                    other, engine = paired
                    await repo.set_idle(conn, engine)
                    outbox.append((engine, ChatEvent(type="partner_left", reason="stopped")))
                    stolen = True
            if other is not None:
                await repo.set_chatting(conn, peer, other)
                await repo.set_chatting(conn, other, peer)
                if not stolen:
                    outbox.append((other, ChatEvent(type="partner_found")))
                phase = "chatting"
        await self._flush(outbox)
        if phase == "searching" and self._engine_enabled:
            await self._events.request_engine()
        return StateOut(phase=phase)

    async def save_web_visitor(
        self,
        visitor_id: UUID | None,
        bot_key: str,
        user_id: int | None = None,
    ) -> VisitorOut:
        async with self._pool.connection() as conn:
            saved = await save_web_visitor(conn, visitor_id, bot_key.strip(), user_id)
        return VisitorOut(id=saved.id, user_id=saved.user_id, created=saved.created)

    async def visit(self, peer: Peer, handle: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await repo.touch_user(conn, peer, handle)

    async def register_channel(self, platform: str, external_id: str, title: str) -> None:
        async with self._pool.connection() as conn:
            await repo.register_channel(conn, platform, external_id, title)

    async def state(self, peer: Peer) -> StateOut:
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
        return StateOut(phase=session.phase if session else "idle")

    async def search(self, peer: Peer) -> StateOut:
        """Встаёт в очередь. Прежний собеседник узнаёт, что диалог закончен."""
        return await self._search(peer, "stopped")

    async def report(self, peer: Peer) -> StateOut:
        """Жалоба закрывает диалог и сразу ставит в поиск."""
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
        if session is None or session.phase != "chatting":
            raise NotChattingError
        return await self._search(peer, "reported")

    async def stop(self, peer: Peer) -> StopOut:
        outbox: Outbox = []
        async with self._locked() as conn:
            was_active = await self._detach(conn, peer, "stopped", outbox)
        await self._flush(outbox)
        return StopOut(was_active=was_active)

    async def send_message(self, peer: Peer, text: str) -> MessageOut:
        """Пересылает реплику. Если пара уже распалась, закрывает диалог."""
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
            if session is None or session.phase != "chatting":
                raise NotChattingError
            partner = await self._linked_partner(conn, peer, session)
        if partner is not None:
            await self._events.publish(partner, ChatEvent(type="message", text=text))
            return MessageOut(delivered=True, phase="chatting")
        outbox: Outbox = []
        async with self._locked() as conn:
            await self._detach(conn, peer, "stopped", outbox)
        await self._flush(outbox)
        return MessageOut(delivered=False, phase="idle")

    async def delivery_failed(self, peer: Peer) -> None:
        """Клиент не смог доставить событие этому пользователю: его диалог закрывается."""
        outbox: Outbox = []
        async with self._locked() as conn:
            await self._detach(conn, peer, "undeliverable", outbox)
        await self._flush(outbox)

    async def engine_demand(self) -> EngineDemandOut:
        async with self._pool.connection() as conn:
            waiting, hunting = await repo.engine_demand(conn, self._fallback_after)
        return EngineDemandOut(waiting=waiting, hunting=hunting)

    async def engine_hunt(self, engine: Peer) -> StateOut:
        """Аккаунт chat-engine начал поиск. В общую очередь он не попадает, пару ему даёт attach."""
        outbox: Outbox = []
        async with self._locked() as conn:
            await self._detach(conn, engine, "stopped", outbox)
            await repo.set_searching(conn, engine)
        await self._flush(outbox)
        return StateOut(phase="searching")

    async def engine_attach(self, engine: Peer) -> EngineAttachOut:
        """chat-engine нашёл собеседника: отдаёт его человеку, который ждёт дольше всех.

        Если за это время все ждущие нашли живых собеседников, аккаунт снимается с поиска
        и `attached=false`: engine_bridge закрывает разговор в chat-engine.
        """
        outbox: Outbox = []
        async with self._locked() as conn:
            session = await repo.get_session(conn, engine)
            if session is None or session.phase != "searching":
                return EngineAttachOut(attached=False, phase=session.phase if session else "idle")
            human = await repo.oldest_waiting_human(conn, self._fallback_after)
            if human is None:
                await repo.set_idle(conn, engine)
                return EngineAttachOut(attached=False, phase="idle")
            await repo.set_chatting(conn, engine, human)
            await repo.set_chatting(conn, human, engine)
            outbox.append((human, ChatEvent(type="partner_found")))
        await self._flush(outbox)
        return EngineAttachOut(attached=True, phase="chatting")

    async def stats(self) -> StatsOut:
        now = time.monotonic()
        if self._stats is not None and now - self._stats[0] < _STATS_TTL_SECONDS:
            return self._stats[1]
        async with self._pool.connection() as conn:
            row = await repo.presence_stats(conn, self._online_window)
        humans = PresenceOut(
            online=row["humans_online"],
            searching=row["humans_searching"],
            chatting=row["humans_chatting"],
        )
        bots = PresenceOut(
            online=row["bots_online"],
            searching=row["bots_searching"],
            chatting=row["bots_chatting"],
        )
        stats = StatsOut(
            online=humans.online + bots.online,
            searching=humans.searching + bots.searching,
            chatting=humans.chatting + bots.chatting,
            humans=humans,
            bots=bots,
        )
        self._stats = (now, stats)
        return stats
