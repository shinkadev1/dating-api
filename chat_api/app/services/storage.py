"""Клиент S3. Бакет приватный: объекты читает только этот сервис."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aioboto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# Проверка бакета при старте не должна держать запуск API: при недоступном S3 без лимитов
# botocore ждёт по 60 с на соединение с повторами, и chat-api минутами не слушает порт.
STARTUP_CHECK_SECONDS = 10.0


class StorageError(Exception):
    """Бакет не принял или не удалил объект."""


def _s3_config() -> Config:
    """Beget не принимает CRC32, который boto3 1.36+ шлёт по умолчанию."""
    kwargs: dict[str, Any] = {
        "signature_version": "s3v4",
        "s3": {"addressing_style": "path"},
        "retries": {"max_attempts": 3, "mode": "standard"},
        "connect_timeout": 5,
        "read_timeout": 30,
    }
    try:
        return Config(
            **kwargs,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    except TypeError:
        return Config(**kwargs)


class ObjectStorage:
    def __init__(
        self,
        *,
        endpoint: str | None,
        region: str,
        bucket: str | None,
        access_key: str | None,
        secret_key: str | None,
    ) -> None:
        self._endpoint = endpoint
        self._region = region
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._client_cm: Any = None
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint and self._bucket and self._access_key and self._secret_key and self._client)

    async def open(self) -> None:
        if not (self._endpoint and self._bucket and self._access_key and self._secret_key):
            logger.warning("S3 не настроен: медиа не сохраняется")
            return
        try:
            session = aioboto3.Session()
            self._client_cm = session.client(
                "s3",
                endpoint_url=self._endpoint,
                region_name=self._region,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                config=_s3_config(),
            )
            self._client = await self._client_cm.__aenter__()
        except Exception:
            logger.exception("Не создал клиент S3 для бакета %s", self._bucket)
            await self.close()
            return
        try:
            await asyncio.wait_for(self._client.head_bucket(Bucket=self._bucket), STARTUP_CHECK_SECONDS)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"403", "404", "AccessDenied", "NoSuchBucket", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                logger.error("S3 бакет %s отклонил доступ (%s): медиа выключено", self._bucket, code)
                await self.close()
                return
            # Сеть или таймаут: клиент остаётся, когда S3 вернётся, загрузки заработают без перезапуска API.
            logger.warning("S3 бакет %s недоступен при старте (%s: %s); загрузки будут пробовать снова", self._bucket, type(exc).__name__, exc)
            return
        logger.info("S3 бакет %s готов", self._bucket)

    async def close(self) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
        self._client = None
        self._client_cm = None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        if not self.enabled or self._bucket is None:
            raise StorageError("Хранилище не настроено")
        try:
            await self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ContentLength=len(body),
            )
        except Exception as exc:
            logger.exception("S3 не записал %s", key)
            raise StorageError(key) from exc

    async def get(self, key: str) -> bytes:
        if not self.enabled or self._bucket is None:
            raise StorageError("Хранилище не настроено")
        try:
            response = await self._client.get_object(Bucket=self._bucket, Key=key)
            return await response["Body"].read()
        except Exception as exc:
            logger.exception("S3 не отдал %s", key)
            raise StorageError(key) from exc

    async def delete(self, key: str) -> None:
        if not self.enabled or self._bucket is None:
            return
        try:
            await self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            logger.exception("S3 не удалил %s", key)
