"""Creating processing jobs (shared by uploads, reprocessing, the sweeper and re-indexing)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

from documind.core.config import Settings
from documind.infrastructure.database.models import Document, ProcessingJob


def new_job(
    document: Document,
    settings: Settings,
    *,
    now: datetime,
    requested_by: uuid.UUID | None,
    dispatched: bool,
) -> ProcessingJob:
    """A queued job for the document's current version.

    ``dispatched`` records that the caller will hand it to the queue right after
    commit; the sweeper only re-dispatches after the grace period.
    """
    if document.current_version_id is None:
        raise ValueError("Document has no current version")
    return ProcessingJob(
        id=uuid.uuid4(),
        workspace_id=document.workspace_id,
        document_id=document.id,
        document_version_id=document.current_version_id,
        status="queued",
        max_attempts=settings.processing_max_attempts,
        next_attempt_at=now,
        dispatched_at=now if dispatched else None,
        requested_by_user_id=requested_by,
    )


def retry_delay_seconds(attempt: int, base_seconds: int, *, cap_seconds: int = 3600) -> int:
    """Exponential backoff with +-20% jitter: base, 2*base, 4*base, ... (capped)."""
    delay = min(cap_seconds, base_seconds * 2 ** max(0, attempt - 1))
    return max(1, int(delay * random.uniform(0.8, 1.2)))  # noqa: S311 - jitter, not crypto


def at(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)
