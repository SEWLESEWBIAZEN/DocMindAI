"""Idempotent provisioning of the private application bucket."""

import logging
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class PublicBucketError(RuntimeError):
    """The application bucket has a bucket policy; it must stay private."""


@dataclass(frozen=True, slots=True)
class BucketBootstrapResult:
    bucket: str
    created: bool


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def ensure_private_bucket(client: Any, bucket: str, region: str) -> BucketBootstrapResult:
    """Create ``bucket`` if missing and verify that no bucket policy grants access.

    Buckets are private by default on both S3 and MinIO. The application never
    attaches a bucket policy, so the presence of one indicates manual
    misconfiguration and is treated as an error rather than silently "fixed".
    """
    created = False
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if _error_code(exc) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        params: dict[str, Any] = {"Bucket": bucket}
        if region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": region}
        client.create_bucket(**params)
        created = True
        logger.info("Created object storage bucket", extra={"bucket": bucket})

    try:
        client.get_bucket_policy(Bucket=bucket)
    except ClientError as exc:
        if _error_code(exc) != "NoSuchBucketPolicy":
            raise
    else:
        raise PublicBucketError(
            f"Bucket {bucket!r} has a bucket policy attached; remove it to keep the bucket private"
        )

    return BucketBootstrapResult(bucket=bucket, created=created)
