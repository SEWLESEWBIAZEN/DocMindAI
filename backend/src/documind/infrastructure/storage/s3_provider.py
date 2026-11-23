"""S3-compatible implementation of ObjectStorageProvider (boto3, off the event loop)."""

from __future__ import annotations

import asyncio
from typing import Any, BinaryIO
from urllib.parse import quote

from botocore.exceptions import ClientError

from documind.application.ports import ObjectMetadata, ObjectNotFoundError

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header with an ASCII fallback and a UTF-8 filename*."""
    unsafe = {'"', "\\", ";"}
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in unsafe else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class S3ObjectStorage:
    def __init__(self, client: Any, bucket: str, *, presign_client: Any | None = None) -> None:
        self._client = client
        self._presign = presign_client or client
        self._bucket = bucket

    async def upload(
        self, key: str, file: BinaryIO, *, content_type: str, metadata: dict[str, str] | None = None
    ) -> None:
        file.seek(0)
        await asyncio.to_thread(
            self._client.upload_fileobj,
            file,
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": metadata or {}},
        )

    async def download_to(self, key: str, file: BinaryIO) -> None:
        try:
            await asyncio.to_thread(self._client.download_fileobj, self._bucket, key, file)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in _NOT_FOUND:
                raise ObjectNotFoundError(key) from exc
            raise

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def get_metadata(self, key: str) -> ObjectMetadata:
        try:
            head = await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in _NOT_FOUND:
                raise ObjectNotFoundError(key) from exc
            raise
        return ObjectMetadata(
            key=key,
            size=int(head["ContentLength"]),
            content_type=head.get("ContentType"),
            etag=str(head.get("ETag", "")).strip('"') or None,
            metadata=dict(head.get("Metadata", {})),
        )

    def generate_signed_url(
        self, key: str, *, expires_in: int, download_filename: str, content_type: str
    ) -> str:
        url: str = self._presign.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                # Always a download, with a type we chose: stored files are never
                # rendered as active content on the storage origin.
                "ResponseContentDisposition": content_disposition(download_filename),
                "ResponseContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
        return url
