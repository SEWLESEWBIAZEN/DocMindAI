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


class ProviderAccessError(PermanentProcessingError):
    """The workspace can't use an AI capability (ADR 0019).

    Codes: ``provider_key_required`` (no key and platform AI not allowed),
    ``provider_key_invalid`` (the workspace's key was rejected), and
    ``capability_unavailable``. The message is safe to show to users.
    Permanent: a processing job fails instead of retrying forever.
    """
