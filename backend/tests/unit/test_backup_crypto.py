"""Backup archive encryption (Phase 9)."""

from __future__ import annotations

import io
import os
import struct

import pytest

from documind.ops.backup_crypto import CHUNK, MAGIC, BackupIntegrityError, seal, unseal
from documind.ops.objects import _safe

PASS = b"a long enough backup passphrase"


def roundtrip(data: bytes, passphrase: bytes = PASS) -> bytes:
    sealed = io.BytesIO()
    seal(io.BytesIO(data), sealed, PASS)
    out = io.BytesIO()
    unseal(io.BytesIO(sealed.getvalue()), out, passphrase)
    return out.getvalue()


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 17])
def test_round_trip(size: int) -> None:
    data = os.urandom(size)
    assert roundtrip(data) == data


def test_wrong_passphrase_fails() -> None:
    with pytest.raises(BackupIntegrityError):
        roundtrip(b"secret data", b"another long passphrase!!")


def _chunks(blob: bytes) -> list[bytes]:
    out, pos = [], len(MAGIC) + 16
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos + 12 : pos + 16])
        out.append(blob[pos : pos + 16 + length])
        pos += 16 + length
    return out


def test_reordered_or_dropped_chunks_are_detected() -> None:
    sealed = io.BytesIO()
    seal(io.BytesIO(os.urandom(3 * CHUNK)), sealed, PASS)
    blob = sealed.getvalue()
    header, chunks = blob[: len(MAGIC) + 16], _chunks(blob)
    assert len(chunks) == 3
    for altered in (
        header + chunks[1] + chunks[0] + chunks[2],  # reordered
        header + chunks[0] + chunks[1],  # last chunk dropped (truncation)
        header + chunks[0] + chunks[2],  # middle chunk dropped
    ):
        with pytest.raises(BackupIntegrityError):
            unseal(io.BytesIO(altered), io.BytesIO(), PASS)


def test_short_passphrases_are_refused() -> None:
    with pytest.raises(ValueError, match="16"):
        seal(io.BytesIO(b"x"), io.BytesIO(), b"short")


def test_object_keys_cannot_escape_the_export_directory(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    assert _safe(root, "workspaces/a/b").is_relative_to(root)
    with pytest.raises(ValueError, match="unsafe"):
        _safe(root, "../../etc/passwd")
