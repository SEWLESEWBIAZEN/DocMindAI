"""Azure Blob Storage implementation of ObjectStorageProvider (MI-6, ADR 0021).

Mapping onto the storage profile: ``bucket`` is the container, ``access_key_id``
the storage account name, the vaulted secret the account key, and
``endpoint_url`` the account's blob endpoint (``https://<account>.blob.core.windows.net``
by default; sovereign clouds, Azure Stack and the Azurite emulator use their own).

The synchronous SDK runs off the event loop, like the S3 adapter. Downloads are
signed with short-lived, read-only service SAS tokens that force an attachment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

from documind.application.ports import ObjectMetadata, ObjectNotFoundError
from documind.infrastructure.storage.s3_provider import content_disposition


def default_endpoint(account: str) -> str:
    return f"https://{account}.blob.core.windows.net"


def blob_service(
    endpoint_url: str, account: str, account_key: str, *, timeout: float = 30.0
) -> BlobServiceClient:
    return BlobServiceClient(
        account_url=endpoint_url,
        credential={"account_name": account, "account_key": account_key},
        connection_timeout=timeout,
        read_timeout=timeout,
        retry_total=3,
    )


class AzureBlobStorage:
    def __init__(
        self, service: BlobServiceClient, container: str, account_key: str, *, prefix: str = ""
    ) -> None:
        self._service = service
        self._container = service.get_container_client(container)
        self._container_name = container
        self._key = account_key  # needed to sign SAS URLs; held only in memory
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

    @property
    def bucket(self) -> str:
        return self._container_name

    @property
    def container(self) -> Any:
        return self._container

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def upload(
        self, key: str, file: BinaryIO, *, content_type: str, metadata: dict[str, str] | None = None
    ) -> None:
        file.seek(0)

        def go() -> None:
            self._container.upload_blob(
                self._k(key),
                file,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
                metadata=metadata or {},
            )

        await asyncio.to_thread(go)

    async def download_to(self, key: str, file: BinaryIO) -> None:
        def go() -> None:
            self._container.download_blob(self._k(key)).readinto(file)

        try:
            await asyncio.to_thread(go)
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._container.delete_blob, self._k(key))
        except ResourceNotFoundError:
            return  # idempotent, as the port requires

    async def get_metadata(self, key: str) -> ObjectMetadata:
        try:
            props = await asyncio.to_thread(
                self._container.get_blob_client(self._k(key)).get_blob_properties
            )
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        return ObjectMetadata(
            key=key,
            size=int(props.size),
            content_type=props.content_settings.content_type,
            etag=str(props.etag or "").strip('"') or None,
            metadata=dict(props.metadata or {}),
        )

    def generate_signed_url(
        self, key: str, *, expires_in: int, download_filename: str, content_type: str
    ) -> str:
        blob = self._container.get_blob_client(self._k(key))
        now = datetime.now(UTC)
        sas = generate_blob_sas(
            account_name=str(self._service.account_name),
            container_name=self._container_name,
            blob_name=self._k(key),
            account_key=self._key,
            permission=BlobSasPermissions(read=True),
            start=now - timedelta(minutes=5),  # clock skew
            expiry=now + timedelta(seconds=expires_in),
            # Always a download with a type we chose, as for S3.
            content_disposition=content_disposition(download_filename),
            content_type=content_type,
        )
        return f"{blob.url}?{sas}"

    def is_public(self) -> bool:
        """Containers with anonymous read access are refused for documents."""
        props = self._container.get_container_properties()
        return bool(props.public_access)

    def close(self) -> None:
        self._service.close()
