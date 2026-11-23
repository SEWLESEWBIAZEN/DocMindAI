"""Document processing lifecycle."""

from enum import StrEnum


class DocumentStatus(StrEnum):
    # Stored and waiting for the processing pipeline (Phase 4 picks these up;
    # the status itself is the durable "to do" record, so no upload is lost
    # if job dispatch fails).
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
