"""Object storage per infrastructure profile (MI-2, ADR 0021)."""

from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from documind.core.config import Settings
from documind.domain.infrastructure.profiles import StorageConfig
from documind.infrastructure.network.ssrf import BlockedHostError, check_url
from documind.infrastructure.storage.s3_provider import S3ObjectStorage


def _client(settings: Settings, config: StorageConfig, secret: str) -> Any:
    return boto3.session.Session().client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if config.force_path_style else "auto"},
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


@dataclass(slots=True)
class BuiltStorage:
    storage: S3ObjectStorage
    client: Any

    def close(self) -> None:
        self.client.close()


def build_storage(settings: Settings, config: StorageConfig, secret: str) -> BuiltStorage:
    """A storage adapter for a customer bucket. The secret is held only by boto3."""
    if config.provider == "documind" or not config.bucket:
        raise ValueError("platform storage is built by the container")
    client = _client(settings, config, secret)
    return BuiltStorage(S3ObjectStorage(client, config.bucket, prefix=config.prefix), client)


@dataclass(frozen=True, slots=True)
class StorageCheck:
    ok: bool
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


async def validate_storage(settings: Settings, config: StorageConfig, secret: str) -> StorageCheck:
    """Endpoint policy, then write, read and delete a probe object under the prefix."""
    allowed = frozenset(settings.infrastructure_allowed_private_hosts)
    if config.endpoint_url:
        try:
            await check_url(config.endpoint_url, allowed=allowed)
        except BlockedHostError as exc:
            return StorageCheck(False, "endpoint_not_allowed", str(exc))
    built = build_storage(settings, config, secret)
    probe = f".documind-probe/{uuid.uuid4()}"
    try:
        payload = b"documind storage check"
        await built.storage.upload(
            probe, io.BytesIO(payload), content_type="text/plain", metadata={"probe": "1"}
        )
        buffer = io.BytesIO()
        await built.storage.download_to(probe, buffer)
        if buffer.getvalue() != payload:
            return StorageCheck(False, "storage_inconsistent", "The probe object didn't read back.")
        await built.storage.delete(probe)
        public = await _is_public(built.client, config.bucket or "")
        if public:
            return StorageCheck(
                False,
                "bucket_public",
                "This bucket is public. Make it private before using it for documents.",
            )
        return StorageCheck(True, details={"bucket": config.bucket, "prefix": config.prefix})
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "error"))
        friendly = {
            "NoSuchBucket": "The bucket doesn't exist.",
            "AccessDenied": "The credentials can't read and write this bucket.",
            "InvalidAccessKeyId": "The access key isn't valid.",
            "SignatureDoesNotMatch": "The secret key doesn't match the access key.",
        }
        return StorageCheck(
            False,
            "storage_rejected",
            friendly.get(code, "The storage service refused the request."),
        )
    except (BotoCoreError, OSError):
        return StorageCheck(
            False, "storage_unreachable", "The storage endpoint couldn't be reached."
        )
    finally:
        built.close()


async def _is_public(client: Any, bucket: str) -> bool:
    """Best effort: S3 reports policy status; other services may not support it."""
    try:
        status = await asyncio.to_thread(client.get_bucket_policy_status, Bucket=bucket)
    except (ClientError, BotoCoreError):
        return False
    return bool(status.get("PolicyStatus", {}).get("IsPublic"))
