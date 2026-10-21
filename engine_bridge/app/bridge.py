"""Мост: аккаунты chat-engine становятся собеседниками людей из Chat API, когда живых в очереди нет.

Порядок для одного человека:
1. Человек встаёт в поиск, живых в очереди нет — Chat API публикует `demand`.
2. Мост отмечает свободный аккаунт `hunt` и запускает поиск в chat-engine.
3. chat-engine нашёл собеседника — мост вызывает `attach`. Если человек за это время
   нашёл живого, `attach` отвечает отказом и мост закрывает разговор в chat-engine.
4. Дальше реплики идут через обычные сообщения Chat API и chat-engine, уход любой
   стороны закрывает диалог у другой.

Мост считает аккаунты своими: найденный не по его запросу собеседник тоже закрывается.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field

from app.chat_api import ChatApiClient, ChatApiError, NotChattingError
from app.engine import EngineClient, EngineConflictError, EngineError

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SECONDS = 3.0

_MEDIA_LABELS = {
    "photo": "[фото]",
    "video": "[видео]",
    "video_note": "[кружок]",
    "voice": "[голосовое]",
}


def _media_label(kind: str, method: str = "") -> str:
    """chat-engine принимает только текст, поэтому файл уходит короткой пометкой."""
    if kind == "voice" and method == "audio":
        return "[аудио]"
    return _MEDIA_LABELS.get(kind, "[медиа]")

# Служебные фразы анонимного бота (@AnonRuBot и похожие). Их нельзя отдавать человеку.
# Уход собеседника закрывает диалог. Реклама и меню — нет: в «Собеседник найден»
# есть ссылка t.me/anon…, и /stop по ней обрывал каждый новый диалог.
_PARTNER_LEFT_RE = re.compile(
    r"закончил(?:а|и)? с вами связь|"
    r"закончил(?:а|и)? связь|"
    r"собеседник (?:покинул|вышел|отключ|закончил)|"
    r"диалог (?:завершен|завершён|окончен)|"
    r"partner left|chat (?:ended|stopped)",
    re.IGNORECASE,
)
_ENGINE_SYSTEM_RE = re.compile(
    r"хотите больше собеседников|"
    r"оставьте мнение о вашем собеседнике|"
    r"напишите\s*/search|"
    r"анонимн\w+\s+чат|"
    r"t\.me/anon|"
    r"@anonim|"
    r"@anonrusupport|"
    r"хочет отправить вам фото|"
    r"нажмите на фото|"
    r"не скрывать для этого диалога",
    re.IGNORECASE,
)


def classify_engine_text(text: str) -> str:
    """leave — собеседник ушёл; drop — меню и реклама бота; forward — реплика человека."""
    if _PARTNER_LEFT_RE.search(text):
        return "leave"
    if _ENGINE_SYSTEM_RE.search(text):
        return "drop"
    return "forward"


@dataclass(eq=False)
class AccountLink:
    id: str
    # Состояние в chat-engine. None, пока WebSocket не прислал snapshot.
    state: str | None = None
    # После ухода собеседника в chat-engine ещё приходят реклама и опрос — их не пересылаем.
    closing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None


class Bridge:
    def __init__(
        self,
        chat_api: ChatApiClient,
        engine: EngineClient,
        *,
        reconcile_interval: float,
        accounts_refresh: float,
        heartbeat: float,
    ) -> None:
        self._chat_api = chat_api
        self._engine = engine
        self._reconcile_interval = reconcile_interval
        self._accounts_refresh = accounts_refresh
        self._heartbeat = heartbeat
        self._links: dict[str, AccountLink] = {}
        self._wake = asyncio.Event()

    def request_reconcile(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        loops = [
            asyncio.create_task(self._accounts_loop(), name="engine-accounts"),
            asyncio.create_task(self._reconcile_loop(), name="engine-reconcile"),
            asyncio.create_task(self._heartbeat_loop(), name="engine-heartbeat"),
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            for task in [*loops, *(link.task for link in self._links.values() if link.task)]:
                task.cancel()

    async def handle_chat_event(self, body: dict) -> None:
        """Событие из потока `chat:events:engine`."""
        kind = body.get("type")
        if kind == "demand":
            self.request_reconcile()
            return
        account = str((body.get("to") or {}).get("bot_key") or "")
        link = self._links.get(account)
        if link is None:
            logger.warning("Событие %s для неизвестного аккаунта %r", kind, account)
            return
        async with link.lock:
            if kind == "message":
                await self._forward_to_engine(link, str(body.get("text") or ""))
            elif kind == "media":
                media_kind = str(body.get("kind") or "")
                media_id = str(body.get("media_id") or "")
                if media_kind == "photo" and media_id:
                    sent = await self._forward_photo_to_engine(
                        link,
                        media_id,
                        str(body.get("content_type") or "image/jpeg"),
                    )
                    if sent:
                        return
                await self._forward_to_engine(link, _media_label(media_kind, str(body.get("tg_method") or "")))
            elif kind == "partner_left":
                await self._stop_engine(link)

    async def _accounts_loop(self) -> None:
        while True:
            try:
                await self._refresh_accounts()
            except EngineError as exc:
                logger.warning("Не получил аккаунты chat-engine: %s", exc)
            await asyncio.sleep(self._accounts_refresh)

    async def _refresh_accounts(self) -> None:
        authorized = {account.id for account in await self._engine.accounts() if account.authorized}
        for account in authorized - self._links.keys():
            link = AccountLink(account)
            link.task = asyncio.create_task(self._follow(link), name=f"engine-ws-{account}")
            self._links[account] = link
            logger.info("Аккаунт chat-engine подключён: %s", account)
        for account in self._links.keys() - authorized:
            link = self._links.pop(account)
            if link.task:
                link.task.cancel()
            logger.info("Аккаунт chat-engine больше не доступен: %s", account)

    async def _follow(self, link: AccountLink) -> None:
        """Держит WebSocket аккаунта и обрабатывает его события по порядку."""
        while True:
            try:
                async for event in self._engine.events(link.id):
                    async with link.lock:
                        await self._on_engine_event(link, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — обрыв соединения не должен останавливать мост
                logger.warning("WebSocket аккаунта %s: %s: %s", link.id, type(exc).__name__, exc)
            link.state = None
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    async def _on_engine_event(self, link: AccountLink, event: dict) -> None:
        kind = event.get("type")
        if kind == "error":
            logger.warning("chat-engine %s: %s", link.id, event.get("detail"))
            return
        state = event.get("state")
        if kind == "snapshot":
            link.state = state
            await self._sync(link)
            return
        if kind == "state":
            previous, link.state = link.state, state
            if state != previous:
                await self._on_state(link, state)
            return
        if kind == "message":
            message = event.get("message") or {}
            photo_id = str(message.get("photo_id") or "")
            if message.get("direction") == "in" and photo_id:
                if link.closing:
                    return
                link.state = state
                if state == "chatting":
                    await self._forward_photo_to_human(link, photo_id)
                return
            text = str(message.get("text") or "")
            incoming = message.get("direction") == "in" and bool(text)
            kind_text = classify_engine_text(text) if incoming else "forward"
            if kind_text == "leave":
                logger.info("chat-engine %s: собеседник завершил диалог", link.id)
                await self._end_from_engine(link)
                return
            if kind_text == "drop":
                # Меню и реклама не закрывают диалог. К chatting можно перейти,
                # если событие state ещё не пришло; в idle из рекламы не уходим.
                logger.info("Служебное сообщение chat-engine %s, диалог не закрываю", link.id)
                if state == "chatting":
                    await self._apply_state(link, "chatting")
                return
            if incoming and link.closing:
                return
            link.state = state
            if state == "chatting" and incoming:
                await self._forward_to_human(link, text)

    async def _sync(self, link: AccountLink) -> None:
        """После (пере)подключения приводит Chat API и chat-engine к одному состоянию."""
        try:
            phase = await self._chat_api.phase(link.id)
        except ChatApiError as exc:
            logger.warning("Не сверил аккаунт %s: %s", link.id, exc)
            return
        logger.info("Аккаунт %s: chat-engine=%s, Chat API=%s", link.id, link.state, phase)
        if link.state == "idle":
            if phase != "idle":
                await self._end_from_engine(link)
            else:
                link.closing = False
            self.request_reconcile()
        elif link.state == "chatting" and phase == "searching":
            await self._on_state(link, "chatting")
        elif link.state != phase:
            await self._stop_engine(link)

    async def _apply_state(self, link: AccountLink, state: str | None) -> None:
        """Переход состояния, если его ещё не обработали отдельным событием state."""
        if not state or state == link.state:
            return
        link.state = state
        await self._on_state(link, state)

    async def _on_state(self, link: AccountLink, state: str | None) -> None:
        if state == "chatting":
            link.closing = False
            try:
                attached = await self._chat_api.attach(link.id)
            except ChatApiError as exc:
                logger.warning("Не привязал собеседника аккаунта %s: %s", link.id, exc)
                attached = False
            logger.info("Аккаунт %s нашёл собеседника, привязан к человеку: %s", link.id, attached)
            if not attached:
                await self._stop_engine(link)
        elif state == "idle":
            await self._end_from_engine(link)

    async def _end_from_engine(self, link: AccountLink) -> None:
        """Собеседник в chat-engine ушёл: человеку только partner_left, служебные фразы не шлём."""
        if link.closing and link.state == "idle":
            return
        link.closing = True
        link.state = "idle"
        await self._stop_human_side(link)
        await self._stop_engine(link)
        self.request_reconcile()

    async def _forward_photo_to_human(self, link: AccountLink, photo_id: str) -> None:
        """Фото из анонимного чата уходит человеку файлом, без подписи «нажмите на фото»."""
        try:
            data, content_type = await self._engine.fetch_photo(link.id, photo_id)
        except EngineError as exc:
            logger.warning("Не скачал фото аккаунта %s: %s", link.id, exc)
            return
        try:
            delivered = await self._chat_api.send_photo(link.id, data, content_type)
        except NotChattingError:
            delivered = False
        except ChatApiError as exc:
            logger.warning("Не передал фото аккаунта %s: %s", link.id, exc)
            return
        if not delivered:
            await self._stop_engine(link)

    async def _forward_photo_to_engine(self, link: AccountLink, media_id: str, content_type: str) -> bool:
        """Фото человека уходит в анонимный чат файлом. False — остаётся текстовая пометка."""
        try:
            data, stored_type = await self._chat_api.download_media(media_id)
        except ChatApiError as exc:
            logger.warning("Не скачал фото %s для %s: %s", media_id, link.id, exc)
            return False
        try:
            await self._engine.send_photo(link.id, data, stored_type or content_type)
        except EngineError as exc:
            logger.warning("chat-engine не принял фото для %s: %s", link.id, exc)
            await self._stop_human_side(link)
        return True

    async def _forward_to_human(self, link: AccountLink, text: str) -> None:
        try:
            delivered = await self._chat_api.send_message(link.id, text)
        except NotChattingError:
            delivered = False
        except ChatApiError as exc:
            logger.warning("Не передал реплику аккаунта %s: %s", link.id, exc)
            return
        if not delivered:
            await self._stop_engine(link)

    async def _forward_to_engine(self, link: AccountLink, text: str) -> None:
        if not text:
            return
        try:
            await self._engine.send(link.id, text)
        except EngineError as exc:
            logger.warning("chat-engine не принял реплику для %s: %s", link.id, exc)
            await self._stop_human_side(link)

    async def _stop_engine(self, link: AccountLink) -> None:
        """Закрывает поиск или разговор в chat-engine. Событие idle затем закроет сторону человека."""
        try:
            await self._engine.stop(link.id)
        except EngineConflictError:
            pass
        except EngineError as exc:
            logger.warning("Не остановил аккаунт %s в chat-engine: %s", link.id, exc)

    async def _stop_human_side(self, link: AccountLink) -> None:
        """Снимает аккаунт в Chat API. Человек в паре с ним получит partner_left."""
        try:
            await self._chat_api.stop(link.id)
        except ChatApiError as exc:
            logger.warning("Не снял аккаунт %s в Chat API: %s", link.id, exc)

    async def _reconcile_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._reconcile_interval)
            self._wake.clear()
            try:
                await self._reconcile()
            except ChatApiError as exc:
                logger.warning("Не сверил очередь: %s", exc)

    async def _reconcile(self) -> None:
        """Число ищущих аккаунтов подгоняется под число людей, которым не нашлось живой пары."""
        demand = await self._chat_api.demand()
        need = demand.waiting - demand.hunting
        if need > 0:
            for link in [link for link in self._links.values() if link.state == "idle"][:need]:
                await self._start_hunt(link)
        elif need < 0:
            for link in [link for link in self._links.values() if link.state == "searching"][:-need]:
                async with link.lock:
                    if link.state == "searching":
                        logger.info("Аккаунт %s больше не нужен, поиск отменён", link.id)
                        await self._stop_engine(link)

    async def _start_hunt(self, link: AccountLink) -> None:
        async with link.lock:
            if link.state != "idle":
                return
            try:
                await self._chat_api.hunt(link.id)
            except ChatApiError as exc:
                logger.warning("Не отметил поиск аккаунта %s: %s", link.id, exc)
                return
            try:
                await self._engine.start(link.id)
            except EngineError as exc:
                logger.warning("chat-engine не начал поиск для %s: %s", link.id, exc)
                await self._stop_human_side(link)
                return
            link.closing = False
            link.state = "searching"
            logger.info("Аккаунт %s ищет собеседника для очереди", link.id)

    async def _heartbeat_loop(self) -> None:
        while True:
            for link in list(self._links.values()):
                if link.state is None:
                    continue
                try:
                    await self._chat_api.visit(link.id)
                except ChatApiError as exc:
                    logger.warning("Не отметил визит аккаунта %s: %s", link.id, exc)
            await asyncio.sleep(self._heartbeat)
