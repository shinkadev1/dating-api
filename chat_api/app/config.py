"""Настройки сервиса из переменных окружения."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    chat_api_service_token: str = Field(min_length=16)
    chat_api_app_token: str = Field(min_length=16)
    # Токен engine_bridge. Без него собеседники из chat-engine не подключаются.
    chat_api_engine_token: str | None = Field(None, min_length=16)
    # Токен бота Telegram на отдельном сервере: действует только за пользователей tg.
    chat_api_tg_token: str | None = Field(None, min_length=16)
    # Токен Bot CRM: только рассылки (/broadcasts). Без него CRM рассылками не управляет.
    chat_api_crm_token: str | None = Field(None, min_length=16)
    # Сколько человек ждёт живого собеседника, прежде чем ему подберут бота из chat-engine.
    engine_fallback_after_seconds: float = Field(0, ge=0)
    # Кто писал за это время, считается онлайн в /stats.
    stats_online_window_seconds: float = Field(300, gt=0)
    events_maxlen: int = 10_000

    # S3-совместимое хранилище медиа. Без любого из значений загрузка выключена.
    s3_endpoint_url: str | None = None
    s3_region: str = "ru1"
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # Сколько дней реплики лежат в базе; старше выгружаются в S3 (archive/messages/) и удаляются.
    messages_hot_days: int = Field(90, ge=1)
    # Ежесуточная выгрузка старых реплик. Без S3 она ничего не делает.
    messages_archive_enabled: bool = True

    # Ссылка на оплату премиума. Тот же секрет задан у premium_site.
    # Без секрета маршрут .../premium/link отвечает 503.
    premium_link_secret: str | None = Field(None, min_length=32)
    pay_public_url: str = "https://pay.chatwithyou.site"

    # Нужен только для однократного переноса старых зашифрованных ключей сообществ ВК в channels.token при старте.
    channel_token_key: str | None = Field(None, min_length=32)

    @field_validator(
        "chat_api_engine_token",
        "chat_api_tg_token",
        "chat_api_crm_token",
        "s3_endpoint_url",
        "s3_bucket",
        "s3_access_key",
        "s3_secret_key",
        "premium_link_secret",
        "channel_token_key",
        mode="before",
    )
    @classmethod
    def _empty_as_unset(cls, value: object) -> object:
        """Compose передаёт незаданную переменную пустой строкой."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("s3_region", mode="before")
    @classmethod
    def _region(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            return "ru1"
        return value.strip()

    @field_validator("pay_public_url", mode="before")
    @classmethod
    def _pay_url(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            return "https://pay.chatwithyou.site"
        return value.strip().rstrip("/")

    @field_validator("s3_endpoint_url", mode="after")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @property
    def engine_enabled(self) -> bool:
        return bool(self.chat_api_engine_token)

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_endpoint_url and self.s3_bucket and self.s3_access_key and self.s3_secret_key)

    @model_validator(mode="after")
    def _distinct_tokens(self) -> Settings:
        optional = [
            self.chat_api_engine_token,
            self.chat_api_tg_token,
            self.chat_api_crm_token,
            self.premium_link_secret,
        ]
        tokens = [self.chat_api_service_token, self.chat_api_app_token, *filter(None, optional)]
        if len(set(tokens)) != len(tokens):
            raise ValueError("Токены CHAT_API_*_TOKEN и PREMIUM_LINK_SECRET должны различаться")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
