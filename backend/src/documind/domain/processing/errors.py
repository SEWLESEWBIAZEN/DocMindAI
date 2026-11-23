"""Processing failures are either permanent (retrying won't help) or transient."""

from __future__ import annotations


class ProcessingError(Exception):
    code = "processing_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class PermanentProcessingError(ProcessingError):
    """The input can't be processed as is (corrupt, encrypted, empty, too large)."""

    code = "unprocessable"


class TransientProcessingError(ProcessingError):
    """A dependency failed temporarily (rate limit, timeout, network); retry later."""

    code = "transient"
