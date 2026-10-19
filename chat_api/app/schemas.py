"""Pydantic-модели запросов, ответов и событий."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Phase = Literal["idle", "searching", "chatting"]
LeftReason = Literal["stopped", "reported", "undeliverable"]


class StateOut(BaseModel):
    phase: Phase = Field(description="`idle` — вне чата, `searching` — в очереди, `chatting` — в диалоге")


class VisitorIn(BaseModel):
    id: UUID | None = Field(default=None, description="Кука iskra_id, если браузер её уже прислал")
    bot_key: str = Field(default="site", min_length=1, max_length=64, description="Ключ сайта, как в адресе peer")
    user_id: int | None = Field(
        default=None,
        gt=0,
        le=2**48 - 1,
        description="Гость чата, к которому воркер привязывает новую куку",
    )


class VisitorOut(BaseModel):
    id: UUID = Field(description="Кука, которую нужно оставить браузеру")
    user_id: int = Field(description="Пользователь web в bot_users")
    created: bool = Field(description="True, если эта кука записана впервые")


class VisitIn(BaseModel):
    handle: str | None = Field(
        default=None,
        max_length=64,
        description="Публичное имя. Для Telegram — @username",
    )


class ChannelIn(BaseModel):
    platform: Literal["vk", "tg", "web"]
    external_id: str = Field(min_length=1, max_length=64, description="Id сообщества ВК, бота Telegram или site")
    title: str = Field(default="", max_length=255, description="Название чата. Пустое не затирает уже записанное")


class StopOut(BaseModel):
    phase: Phase = "idle"
    was_active: bool = Field(description="Был ли поиск или диалог до остановки")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class MessageOut(BaseModel):
    delivered: bool = Field(description="Сообщение передано собеседнику")
    phase: Phase


MediaKind = Literal["photo", "video", "video_note", "voice"]


class MediaOut(BaseModel):
    """Файл лежит в S3. Собеседнику он этим запросом не отправляется."""

    id: UUID
    kind: MediaKind
    key: str = Field(description="Ключ объекта в бакете")
    content_type: str
    size: int = Field(description="Размер в байтах")


class ChatEvent(BaseModel):
    """Событие для второй стороны. Инициатор узнаёт итог из HTTP-ответа."""

    type: Literal["partner_found", "message", "partner_left", "notice"]
    text: str | None = Field(None, description="Текст для `message` и `notice`")
    reason: LeftReason | None = Field(None, description="Причина для `partner_left`")


class NoticeIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096, description="Текст, который получат диалоги бота")


class NoticeOut(BaseModel):
    queued: int = Field(description="Сколько диалогов получат текст")


class EngineDemandOut(BaseModel):
    waiting: int = Field(description="Людей в очереди дольше порога подбора бота")
    hunting: int = Field(description="Аккаунтов chat-engine, которые уже ищут собеседника")


class EngineAttachOut(BaseModel):
    attached: bool = Field(description="Аккаунт chat-engine связан с ждущим человеком")
    phase: Phase = Field(description="Фаза аккаунта chat-engine после вызова")


class PresenceOut(BaseModel):
    online: int = Field(description="В поиске, в диалоге или писали за последние минуты")
    searching: int
    chatting: int


class StatsOut(BaseModel):
    """Счётчики для сайта. Общие числа включают аккаунты chat-engine."""

    online: int
    searching: int
    chatting: int
    humans: PresenceOut
    bots: PresenceOut
