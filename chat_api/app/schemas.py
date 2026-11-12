"""Pydantic-модели запросов, ответов и событий."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Phase = Literal["idle", "searching", "chatting"]
LeftReason = Literal["stopped", "reported", "undeliverable"]


ChatMode = Literal["normal", "adult"]


class SearchIn(BaseModel):
    mode: ChatMode = Field(default="normal", description="`normal` или `adult` («Пошлый чат», только с премиумом)")


class StateOut(BaseModel):
    phase: Phase = Field(description="`idle` — вне чата, `searching` — в очереди, `chatting` — в диалоге")


class PremiumLinkOut(BaseModel):
    url: str = Field(description="Страница оплаты /checkout/<подписанный токен>; действует 24 часа")
    premium_active: bool = Field(description="Премиум действует сейчас")
    premium_until: datetime | None = Field(description="До какого момента оплачен премиум")


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
    promo: bool = Field(False, description="Показать инициатору «Диалог остановлен» с предложением премиума")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class MessageOut(BaseModel):
    delivered: bool = Field(description="Сообщение передано собеседнику")
    phase: Phase


MediaKind = Literal["photo", "video", "video_note", "voice"]
TgSend = Literal["photo", "audio", "voice", "video", "video_note"]


class MediaOut(BaseModel):
    """Файл лежит в S3 и поставлен в очередь собеседнику."""

    id: UUID
    kind: MediaKind
    key: str = Field(description="Ключ объекта в бакете")
    content_type: str
    size: int = Field(description="Размер в байтах")


class ChatEvent(BaseModel):
    """Событие для второй стороны. Инициатор узнаёт итог из HTTP-ответа."""

    type: Literal["partner_found", "message", "partner_left", "notice", "media"]
    text: str | None = Field(None, description="Текст для `message` и `notice`")
    reason: LeftReason | None = Field(None, description="Причина для `partner_left`")
    promo: bool | None = Field(None, description="Для partner_left: показать экран конца диалога с предложением премиума (только vk/tg)")
    media_id: str | None = Field(None, description="Файл для `media`. Байты забирает бот собеседника")
    kind: MediaKind | None = Field(None, description="Вид файла для `media`")
    content_type: str | None = Field(None, description="MIME для `media`")
    locked: bool | None = Field(
        None,
        description="Для `media`: фото размыто, получатель без премиума. Оригинала и tg_file_id в событии нет",
    )
    tg_file_id: str | None = Field(None, description="Файл, который бот Telegram уже получил. Действует только у этого бота")
    tg_method: TgSend | None = Field(
        None,
        description="Как отправить tg_file_id: photo, audio, voice, video или video_note",
    )


class NoticeIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096, description="Текст, который получат диалоги бота")


class NoticeOut(BaseModel):
    queued: int = Field(description="Сколько диалогов получат текст")


class ModeDemandOut(BaseModel):
    waiting: int = 0
    hunting: int = 0


class EngineDemandOut(BaseModel):
    waiting: int = Field(description="Людей в очереди дольше порога подбора бота, все режимы")
    hunting: int = Field(description="Аккаунтов chat-engine, которые уже ищут собеседника, все режимы")
    modes: dict[str, ModeDemandOut] = Field(
        default_factory=dict, description="То же по режимам: ключи `normal` и `adult`"
    )


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


BroadcastPlatform = Literal["vk", "tg", "web"]
BroadcastEvent = Literal["first_seen", "last_seen", "dialog_ended", "premium_ended"]
BroadcastStatus = Literal["scheduled", "sending", "done", "active", "paused", "cancelled"]


class BroadcastIn(BaseModel):
    title: str = Field("", max_length=200, description="Название для CRM; пользователи его не видят")
    text: str = Field(min_length=1, max_length=4096, description="Текст уведомления")
    platforms: list[BroadcastPlatform] = Field(min_length=1, description="Кому: vk, tg, web")
    kind: Literal["at", "event"] = Field(description="`at` — один раз в send_at, `event` — после события")
    send_at: datetime | None = Field(None, description="Для `at`: когда отправить; в прошлом — сразу")
    event: BroadcastEvent | None = Field(
        None,
        description="Для `event`: first_seen — первый раз написал, last_seen — последняя активность, "
        "dialog_ended — закончился диалог, premium_ended — истёк премиум",
    )
    delay_minutes: int | None = Field(None, ge=0, le=525_600, description="Для `event`: пауза после события")
    repeat: bool = Field(False, description="Для `event`: после каждого нового события, а не один раз")


class BroadcastPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    text: str | None = Field(None, min_length=1, max_length=4096)
    platforms: list[BroadcastPlatform] | None = Field(None, min_length=1)
    send_at: datetime | None = None
    delay_minutes: int | None = Field(None, ge=0, le=525_600)
    repeat: bool | None = None
    status: Literal["scheduled", "active", "paused", "cancelled"] | None = Field(
        None, description="paused/active — пауза рассылки после события, cancelled — отменить"
    )


class BroadcastOut(BaseModel):
    id: int
    title: str
    text: str
    has_image: bool
    platforms: list[str]
    kind: Literal["at", "event"]
    send_at: datetime | None
    event: str | None
    delay_minutes: int | None
    repeat: bool
    status: BroadcastStatus
    sent_count: int = Field(description="Сколько получателей поставлено в очередь")
    created_at: datetime
    updated_at: datetime
    active_since: datetime | None = Field(description="Для `event`: учитываются события не раньше этого момента")
    finished_at: datetime | None


class AudienceOut(BaseModel):
    recipients: int = Field(description="Сколько пар «человек — чат» получат рассылку по времени сейчас")


class UserStatsOut(BaseModel):
    total: int = Field(description="Людей на выбранных площадках")
    premium: int = Field(description="С действующим премиумом")
    active_24h: int
    active_7d: int
    new_24h: int
    new_7d: int
    searching: int = Field(description="Сейчас в поиске")
    chatting: int = Field(description="Сейчас в диалоге")


class UserRowOut(BaseModel):
    platform: str
    user_id: int
    handle: str | None
    search_count: int
    started_at: datetime
    last_seen_at: datetime
    premium_until: datetime | None
    phase: Phase
    channels: int = Field(description="Через сколько чатов (сообществ, ботов) писал")


class UserListOut(BaseModel):
    total: int
    items: list[UserRowOut]


class UserChatOut(BaseModel):
    platform: str
    external_id: str
    title: str
    started_at: datetime
    last_seen_at: datetime
    search_count: int


class UserBroadcastOut(BaseModel):
    id: int
    title: str
    text: str
    sent_at: datetime
    times: int


class UserDetailOut(UserRowOut):
    channels: int = 0
    chats: list[UserChatOut]
    dialogs: int = Field(description="Сколько было диалогов")
    last_dialog_at: datetime | None
    reported: int = Field(description="Диалогов, закончившихся жалобой")
    broadcasts: list[UserBroadcastOut] = Field(description="Какие рассылки получил (последние 30)")
