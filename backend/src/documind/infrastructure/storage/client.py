"""boto3 S3 client factory for S3-compatible storage (MinIO locally, S3 in the cloud).

The full ``ObjectStorageProvider`` abstraction is introduced in Phase 3; this
module only owns client construction so configuration stays in one place.
"""

from typing import Any

import boto3
from botocore.config import Config

from documind.core.config import Settings


def create_s3_client(settings: Settings) -> Any:
    """Create a thread-safe boto3 S3 client. No network call is made here."""
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
