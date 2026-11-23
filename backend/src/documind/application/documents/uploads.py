"""Inspection of an uploaded file before anything is stored (runs in a worker thread)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import BinaryIO

from documind.domain.documents.files import DocumentFormat, detect, sanitize_filename
from documind.domain.errors import PayloadTooLargeError, ValidationFailedError

_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class InspectedUpload:
    filename: str
    format: DocumentFormat
    media_type: str
    size_bytes: int
    checksum_sha256: str


def inspect_upload(file: BinaryIO, raw_filename: str | None, *, max_bytes: int) -> InspectedUpload:
    """Validate size, type and content; compute the checksum. Blocking I/O."""
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size == 0:
        raise ValidationFailedError("The file is empty.")
    if size > max_bytes:
        raise PayloadTooLargeError(f"Files can be at most {max_bytes // (1024 * 1024)} MB.")

    filename = sanitize_filename(raw_filename)
    detected = detect(file, filename)

    digest = hashlib.sha256()
    while chunk := file.read(_CHUNK):
        digest.update(chunk)
    file.seek(0)
    return InspectedUpload(
        filename=filename,
        format=detected.format,
        media_type=detected.media_type,
        size_bytes=size,
        checksum_sha256=digest.hexdigest(),
    )
