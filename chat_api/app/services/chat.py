"""Логика анонимного чата: очередь поиска, пары и пересылка реплик."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.events import EventBus
from app.peer import Peer
from app.repositories import messages as messages_repo
from app.repositories import sessions as repo
from app.repositories.visitors import save_web_visitor
from app.schemas import (
    ChatEvent,
    MediaKind,
    TgSend,
    EngineAttachOut,
    EngineDemandOut,
    LeftReason,
    ModeDemandOut,
    MessageOut,
    Phase,
    PresenceOut,
    StateOut,
    StatsOut,
    StopOut,
    VisitorOut,
)

logger = logging.getLogger(__name__)

Outbox = list[tuple[Peer, ChatEvent]]

# Счётчики для сайта не обязаны быть точными до секунды, а запрос считает всю таблицу.
_STATS_TTL_SECONDS = 5.0


# Боты узнают отказ по 403 со словом «премиум» в detail и показывают своё предложение купить.
MEDIA_NEEDS_PREMIUM = "Фото, видео и аудио можно отправлять только с премиумом"
PARTNER_NEEDS_PREMIUM = "Собеседник без премиума не может получать медиа"

# Диалоги человека без премиума считаются завершёнными по счётчику: на 5-м, 15-м, 25-м... (n % 10 == 5)
# партнёру в partner_left и инициатору в ответе /stop приходит флаг promo.
PROMO_FIRST = 5
PROMO_EVERY = 10
_PROMO_PLATFORMS = ("vk", "tg")


def _promo_due(count: int) -> bool:
    return count % PROMO_EVERY == PROMO_FIRST


ADULT_NEEDS_PREMIUM = "Пошлый чат доступен только с премиумом"


class AdultNeedsPremiumError(Exception):
    """Режим adult без активного премиума."""


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

    async def _promo_targets(
        self,
        conn: AsyncConnection,
        ended: list[repo.DialogEnd],
        candidates: set[Peer],
    ) -> set[Peer]:
        """Кому показать промо: счётчик на пороге, vk/tg, без премиума. Вызывать до коммита."""
        targets: set[Peer] = set()
        for item in ended:
            peer = item.peer
            if (
                peer in candidates
                and peer not in targets
                and _promo_due(item.dialogs_ended)
                and peer.platform in _PROMO_PLATFORMS
                and not await repo.user_is_premium(conn, peer)
            ):
                targets.add(peer)
        return targets

    @staticmethod
    def _left_recipients(outbox: Outbox) -> set[Peer]:
        return {r for r, ev in outbox if ev.type == "partner_left"}

    @staticmethod
    def _mark_promo(outbox: Outbox, targets: set[Peer]) -> None:
        if not targets:
            return
        outbox[:] = [
            (r, ev.model_copy(update={"promo": True}) if ev.type == "partner_left" and r in targets else ev)
            for r, ev in outbox
        ]

    @staticmethod
    async def _linked_partner(conn: AsyncConnection, peer: Peer, session: repo.SessionRow | None) -> Peer | None:
        """Собеседник, если диалог связан с двух сторон."""
        if session is None or session.phase != "chatting" or session.partner is None:
            return None
        other = await repo.get_session(conn, session.partner)
        if other is None or other.phase != "chatting" or other.partner != peer:
            return None
        return session.partner

    async def _record(self, dialog_id: int | None, peer: Peer, partner: Peer | None, **fields: object) -> None:
        """Пишет реплику в историю. Сбой не меняет ответ клиенту и не раскрывает содержимое в логе."""
        if dialog_id is None:
            return
        try:
            async with self._pool.connection() as conn:
                await messages_repo.insert_message(conn, dialog_id=dialog_id, sender=peer, partner=partner, **fields)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning("Не записал сообщение в диалог %s: %s", dialog_id, type(exc).__name__)

    async def _notify_left(
        self,
        conn: AsyncConnection,
        other: Peer,
        reason: LeftReason,
        outbox: Outbox,
        ended: list[repo.DialogEnd],
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
        ended.extend(await repo.set_idle(conn, other, reason=reason, ended_by=skip) or ())
        outbox.append((other, ChatEvent(type="partner_left", reason=reason)))
        if extra is not None:
            ended.extend(await repo.set_idle(conn, extra, reason=reason, ended_by=skip) or ())
            outbox.append((extra, ChatEvent(type="partner_left", reason=reason)))

    async def _detach(
        self,
        conn: AsyncConnection,
        peer: Peer,
        reason: LeftReason,
        outbox: Outbox,
        ended: list[repo.DialogEnd],
    ) -> bool:
        """Снимает пользователя с поиска или диалога. True, если было что снимать.

        Сообщает собеседнику даже если связь в сессиях уже несимметрична: иначе
        человек, которому нашли пару, не узнает, что второй нажал «Следующий».
        """
        session = await repo.get_session(conn, peer)
        if session is None or session.phase == "idle":
            return False
        partner = session.partner if session.phase == "chatting" else None
        ended.extend(await repo.set_idle(conn, peer, reason=reason, ended_by=peer) or ())
        if partner is not None:
            await self._notify_left(conn, partner, reason, outbox, ended, skip=peer)
        return True

    async def _search(self, peer: Peer, left_reason: LeftReason, mode: str = "normal") -> StateOut:
        outbox: Outbox = []
        ended: list[repo.DialogEnd] = []
        async with self._locked() as conn:
            await self._detach(conn, peer, left_reason, outbox, ended)
            ended.extend(await repo.set_searching(conn, peer, mode) or ())
            await repo.record_search(conn, peer)
            phase: Phase = "searching"
            other = await repo.find_searcher(conn, peer, mode)
            stolen = False
            if other is None and not peer.is_engine:
                paired = await repo.find_human_with_engine(conn, mode)
                if paired is not None:
                    other, engine = paired
                    ended.extend(await repo.set_idle(conn, engine, reason="stopped", ended_by=None) or ())
                    outbox.append((engine, ChatEvent(type="partner_left", reason="stopped")))
                    stolen = True
            if other is not None:
                dialog_id = await repo.open_dialog(conn, peer, other)
                await repo.set_chatting(conn, peer, other, dialog_id)
                await repo.set_chatting(conn, other, peer, dialog_id)
                if not stolen:
                    outbox.append((other, ChatEvent(type="partner_found")))
                phase = "chatting"
            self._mark_promo(outbox, await self._promo_targets(conn, ended, self._left_recipients(outbox)))
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

    async def notify_channels(
        self,
        text: str,
        *,
        media_id: str | None = None,
        content_type: str | None = None,
    ) -> int:
        """Ставит текст и, если есть, фото каждому человеку в каждом чате, где он уже писал боту."""
        async with self._pool.connection() as conn:
            recipients = await repo.channel_recipients(conn)
        await self._events.publish_notices(
            recipients,
            text,
            media_id=media_id,
            content_type=content_type,
        )
        return len(recipients)

    async def state(self, peer: Peer) -> StateOut:
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
        return StateOut(phase=session.phase if session else "idle")

    async def can_send_media(self, peer: Peer) -> bool:
        async with self._pool.connection() as conn:
            return await repo.user_is_premium(conn, peer)

    async def premium_until(self, peer: Peer) -> datetime | None:
        async with self._pool.connection() as conn:
            return await repo.user_premium_until(conn, peer)

    async def media_block_reason(self, peer: Peer, kind: str) -> str | None:
        """Почему медиа не уйдёт. Фото идут всем: получатель без премиума увидит размытую копию.

        Видео, кружок и голос требуют премиума у отправителя и у живого собеседника.
        Собеседник из chat-engine премиума не требует, а сам chat-engine (engine_bridge) шлёт медиа
        без проверки. Если пары уже нет, решает forward_media.
        """
        if peer.is_engine or kind == "photo":
            return None
        async with self._pool.connection() as conn:
            if not await repo.user_is_premium(conn, peer):
                return MEDIA_NEEDS_PREMIUM
            session = await repo.get_session(conn, peer)
            partner = await self._linked_partner(conn, peer, session)
            if partner is not None and not partner.is_engine and not await repo.user_is_premium(conn, partner):
                return PARTNER_NEEDS_PREMIUM
        return None

    async def search(self, peer: Peer, mode: str = "normal") -> StateOut:
        """Встаёт в очередь своего режима. Прежний собеседник узнаёт, что диалог закончен.

        Режим adult требует активного премиума, иначе AdultNeedsPremiumError.
        """
        if mode == "adult":
            async with self._pool.connection() as conn:
                if not await repo.user_is_premium(conn, peer):
                    raise AdultNeedsPremiumError
        return await self._search(peer, "stopped", mode)

    async def report(self, peer: Peer) -> StateOut:
        """Жалоба закрывает диалог и сразу ставит в поиск в том же режиме, что и диалог.

        Если диалог был adult, а премиум уже кончился, поиск идёт в normal: жалоба не должна падать.
        """
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
            if session is None or session.phase != "chatting":
                raise NotChattingError
            mode = session.mode
            if mode == "adult" and not await repo.user_is_premium(conn, peer):
                mode = "normal"
        return await self._search(peer, "reported", mode)

    async def stop(self, peer: Peer) -> StopOut:
        outbox: Outbox = []
        ended: list[repo.DialogEnd] = []
        async with self._locked() as conn:
            was_active = await self._detach(conn, peer, "stopped", outbox, ended)
            targets = await self._promo_targets(conn, ended, self._left_recipients(outbox) | {peer})
            self._mark_promo(outbox, targets)
        await self._flush(outbox)
        return StopOut(was_active=was_active, promo=peer in targets)

    async def send_message(self, peer: Peer, text: str) -> MessageOut:
        """Пересылает реплику. Если пара уже распалась, закрывает диалог."""
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
            if session is None or session.phase != "chatting":
                raise NotChattingError
            partner = await self._linked_partner(conn, peer, session)
        if partner is not None:
            await self._events.publish(partner, ChatEvent(type="message", text=text))
            await self._record(session.dialog_id, peer, partner, kind="text", status="delivered", text=text)
            return MessageOut(delivered=True, phase="chatting")
        outbox: Outbox = []
        ended: list[repo.DialogEnd] = []
        async with self._locked() as conn:
            await self._detach(conn, peer, "stopped", outbox, ended)
            self._mark_promo(outbox, await self._promo_targets(conn, ended, self._left_recipients(outbox)))
        await self._flush(outbox)
        await self._record(session.dialog_id, peer, session.partner, kind="text", status="left", text=text)
        return MessageOut(delivered=False, phase="idle")

    async def forward_media(
        self,
        peer: Peer,
        media_id: UUID | None,
        kind: MediaKind,
        content_type: str | None,
        *,
        tg_file_id: str | None = None,
        tg_method: TgSend | None = None,
        locked_copy: Callable[[], Awaitable[UUID | None]] | None = None,
    ) -> str:
        """Ставит файл в очередь собеседника.

        `sent` — событие опубликовано. `left` — пары уже нет, диалог закрыт.
        `skipped` — файл не уйдёт: байтов нет, а file_id Telegram собеседнику на другой платформе
        нечего отдать. `locked` — фото живому собеседнику без премиума, а размытой копии нет:
        не уйдёт (в историю пишется со статусом skipped), роутер отвечает отправителю как при успехе.
        Такому собеседнику вместо оригинала уходит размытая копия из `locked_copy` с `locked=true`,
        без tg_file_id; в историю пишется оригинал.
        """
        fields = {
            "kind": "audio" if tg_method == "audio" else kind,
            "media_id": media_id,
            "tg_file_id": tg_file_id,
            "tg_method": tg_method,
            "content_type": content_type,
        }
        async with self._pool.connection() as conn:
            session = await repo.get_session(conn, peer)
            if session is None or session.phase != "chatting":
                return "left"
            partner = await self._linked_partner(conn, peer, session)
        if partner is None:
            outbox: Outbox = []
            ended: list[repo.DialogEnd] = []
            async with self._locked() as conn:
                await self._detach(conn, peer, "stopped", outbox, ended)
                self._mark_promo(outbox, await self._promo_targets(conn, ended, self._left_recipients(outbox)))
            await self._flush(outbox)
            await self._record(session.dialog_id, peer, session.partner, status="left", **fields)
            return "left"
        if media_id is None and partner.platform != "tg":
            logger.info(
                "Файл Telegram без байтов не уходит %s/%s/%s",
                partner.platform,
                partner.bot_key,
                partner.user_id,
            )
            await self._record(session.dialog_id, peer, partner, status="skipped", **fields)
            return "skipped"
        if kind == "photo" and not partner.is_engine:
            async with self._pool.connection() as conn:
                partner_premium = await repo.user_is_premium(conn, partner)
            if not partner_premium:
                copy_id = await locked_copy() if locked_copy is not None else None
                if copy_id is None:
                    await self._record(session.dialog_id, peer, partner, status="skipped", **fields)
                    return "locked"
                await self._events.publish(
                    partner,
                    ChatEvent(
                        type="media",
                        media_id=str(copy_id),
                        kind="photo",
                        content_type="image/jpeg",
                        locked=True,
                    ),
                )
                logger.info(
                    "Размытое фото %s -> %s/%s/%s", copy_id, partner.platform, partner.bot_key, partner.user_id
                )
                await self._record(session.dialog_id, peer, partner, status="delivered", **fields)
                return "sent"
        await self._events.publish(
            partner,
            ChatEvent(
                type="media",
                media_id=str(media_id) if media_id else None,
                kind=kind,
                content_type=content_type,
                tg_file_id=tg_file_id,
                tg_method=tg_method,
            ),
        )
        logger.info(
            "Медиа %s (%s) -> %s/%s/%s",
            media_id or tg_file_id,
            tg_method or kind,
            partner.platform,
            partner.bot_key,
            partner.user_id,
        )
        await self._record(session.dialog_id, peer, partner, status="delivered", **fields)
        return "sent"

    async def delivery_failed(self, peer: Peer) -> None:
        """Клиент не смог доставить событие этому пользователю: его диалог закрывается."""
        outbox: Outbox = []
        ended: list[repo.DialogEnd] = []
        async with self._locked() as conn:
            await self._detach(conn, peer, "undeliverable", outbox, ended)
        await self._flush(outbox)

    async def engine_demand(self) -> EngineDemandOut:
        async with self._pool.connection() as conn:
            by_mode = await repo.engine_demand(conn, self._fallback_after)
        return EngineDemandOut(
            waiting=sum(w for w, _ in by_mode.values()),
            hunting=sum(h for _, h in by_mode.values()),
            modes={mode: ModeDemandOut(waiting=w, hunting=h) for mode, (w, h) in by_mode.items()},
        )

    async def engine_hunt(self, engine: Peer, mode: str = "normal") -> StateOut:
        """Аккаунт chat-engine начал поиск. В общую очередь он не попадает, пару ему даёт attach."""
        outbox: Outbox = []
        ended: list[repo.DialogEnd] = []
        async with self._locked() as conn:
            await self._detach(conn, engine, "stopped", outbox, ended)
            ended.extend(await repo.set_searching(conn, engine, mode) or ())
            self._mark_promo(outbox, await self._promo_targets(conn, ended, self._left_recipients(outbox)))
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
            human = await repo.oldest_waiting_human(conn, self._fallback_after, session.mode)
            if human is None:
                await repo.set_idle(conn, engine, reason="stopped", ended_by=None)
                return EngineAttachOut(attached=False, phase="idle")
            dialog_id = await repo.open_dialog(conn, engine, human)
            await repo.set_chatting(conn, engine, human, dialog_id)
            await repo.set_chatting(conn, human, engine, dialog_id)
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
