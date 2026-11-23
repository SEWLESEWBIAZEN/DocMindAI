"""Object storage health check."""

import asyncio
from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from documind.domain.health import DependencyCheckError, DetailValue


class ObjectStorageHealthCheck:
    name = "object_storage"

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def check(self) -> Mapping[str, DetailValue]:
        try:
            # boto3 is synchronous; keep it off the event loop.
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchBucket", "NotFound"}:
                raise DependencyCheckError(
                    "bucket does not exist; run `documind-manage bootstrap-storage`"
                ) from exc
            if code in {"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise DependencyCheckError("access to bucket denied") from exc
            raise
        return {"bucket": self._bucket}
