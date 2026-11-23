"""Validation of uploaded files. Uploaded content is untrusted.

Detection never trusts the client's Content-Type or the file extension alone:
the extension must be on the allowlist *and* agree with the file's actual
signature and structure. Only the standard library is used.
"""

from __future__ import annotations

import codecs
import unicodedata
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import BinaryIO

from documind.domain.errors import UnsupportedMediaTypeError, ValidationFailedError

MAX_FILENAME_LENGTH = 255
MAX_TITLE_LENGTH = 255
_SNIFF_BYTES = 8192

# DOCX sanity limits. We only read the zip directory and [Content_Types].xml
# here; full decompression (with its own limits) happens in Phase 4 extraction.
_DOCX_MAX_ENTRIES = 5000
_DOCX_MAX_CONTENT_TYPES_BYTES = 1024 * 1024


class DocumentFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MARKDOWN = "markdown"


MEDIA_TYPES: dict[DocumentFormat, str] = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentFormat.TXT: "text/plain; charset=utf-8",
    DocumentFormat.MARKDOWN: "text/markdown; charset=utf-8",
}

_EXTENSIONS: dict[str, DocumentFormat] = {
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".txt": DocumentFormat.TXT,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
}

SUPPORTED_EXTENSIONS = tuple(sorted(_EXTENSIONS))


@dataclass(frozen=True, slots=True)
class DetectedFile:
    format: DocumentFormat
    media_type: str


def _strip_controls(text: str) -> str:
    """Whitespace controls (tab, CR, LF) become spaces; other control/format chars are dropped."""
    return "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )


def sanitize_filename(raw: str | None) -> str:
    """Make a client-supplied filename safe for display and Content-Disposition.

    Directory components (POSIX or Windows), control characters and leading
    dots are removed. The result is never used to build storage paths.
    """
    name = unicodedata.normalize("NFC", raw or "")
    name = PureWindowsPath(PurePosixPath(name.replace("\\", "/")).name).name
    name = " ".join(_strip_controls(name).split()).lstrip(".").strip()
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_FILENAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_FILENAME_LENGTH]
    return name or "document"


def title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return clean_title(stem or filename)


def clean_title(title: str) -> str:
    cleaned = " ".join(_strip_controls(title).split())
    if not cleaned:
        raise ValidationFailedError("Title can't be empty.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValidationFailedError(f"Title must be at most {MAX_TITLE_LENGTH} characters.")
    return cleaned


def format_for_filename(filename: str) -> DocumentFormat:
    suffix = PurePosixPath(filename.lower()).suffix
    fmt = _EXTENSIONS.get(suffix)
    if fmt is None:
        raise UnsupportedMediaTypeError(
            f"Unsupported file type. Upload one of: {', '.join(SUPPORTED_EXTENSIONS)}."
        )
    return fmt


def detect(file: BinaryIO, filename: str) -> DetectedFile:
    """Validate that ``file`` really is the type its (sanitized) name says it is."""
    expected = format_for_filename(filename)
    file.seek(0)
    head = file.read(_SNIFF_BYTES)
    file.seek(0)
    if not head:
        raise ValidationFailedError("The file is empty.")

    if expected is DocumentFormat.PDF:
        _check_pdf(head)
    elif expected is DocumentFormat.DOCX:
        _check_docx(file, head)
    else:
        _check_text(file)
    file.seek(0)
    return DetectedFile(format=expected, media_type=MEDIA_TYPES[expected])


def _mismatch() -> UnsupportedMediaTypeError:
    return UnsupportedMediaTypeError("The file's contents don't match its extension.")


def _check_pdf(head: bytes) -> None:
    # The PDF header must appear within the first 1024 bytes (ISO 32000).
    if b"%PDF-" not in head[:1024]:
        raise _mismatch()


def _check_docx(file: BinaryIO, head: bytes) -> None:
    if not head.startswith(b"PK\x03\x04"):
        raise _mismatch()
    try:
        with zipfile.ZipFile(file) as archive:
            infos = archive.infolist()
            if len(infos) > _DOCX_MAX_ENTRIES:
                raise UnsupportedMediaTypeError("The document has too many internal parts.")
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise _mismatch()
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise UnsupportedMediaTypeError("Macro-enabled documents aren't accepted.")
            info = archive.getinfo("[Content_Types].xml")
            if info.file_size > _DOCX_MAX_CONTENT_TYPES_BYTES:
                raise _mismatch()
            content_types = archive.read(info).decode("utf-8", errors="replace").lower()
    except zipfile.BadZipFile as exc:
        raise _mismatch() from exc
    if "macroenabled" in content_types:
        raise UnsupportedMediaTypeError("Macro-enabled documents aren't accepted.")
    if "wordprocessingml.document.main+xml" not in content_types:
        raise _mismatch()


def _check_text(file: BinaryIO) -> None:
    """Plain text and Markdown must be UTF-8 (BOM allowed) without NUL bytes."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    file.seek(0)
    try:
        while chunk := file.read(1024 * 1024):
            if b"\x00" in chunk:
                raise UnsupportedMediaTypeError("Text files must not contain binary data.")
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise UnsupportedMediaTypeError("Text files must be UTF-8 encoded.") from exc
