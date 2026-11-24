"""Authenticated encryption for backup archives.

Format (version 1)::

    magic "DMBK1" | salt (16) | then chunks:
        nonce (12) | length (4, big endian) | AES-256-GCM(chunk)

The key comes from a passphrase via scrypt (n=2**15, r=8, p=1). Each chunk's
associated data binds its index and whether it is the last one, so chunks
can't be reordered, dropped or truncated without detection.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"DMBK1"
CHUNK = 1024 * 1024


class BackupIntegrityError(Exception):
    """Wrong passphrase, or the archive was altered or truncated."""


def _key(passphrase: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase)


def _aad(index: int, last: bool) -> bytes:
    return MAGIC + struct.pack(">Q?", index, last)


def seal(source: BinaryIO, target: BinaryIO, passphrase: bytes) -> None:
    if len(passphrase) < 16:
        raise ValueError("Use a backup passphrase of at least 16 characters")
    salt = os.urandom(16)
    aead = AESGCM(_key(passphrase, salt))
    target.write(MAGIC + salt)
    index = 0
    chunk = source.read(CHUNK)
    while True:
        following = source.read(CHUNK)
        last = not following
        nonce = os.urandom(12)
        sealed = aead.encrypt(nonce, chunk, _aad(index, last))
        target.write(nonce + struct.pack(">I", len(sealed)) + sealed)
        if last:
            return
        chunk, index = following, index + 1


def unseal(source: BinaryIO, target: BinaryIO, passphrase: bytes) -> None:
    header = source.read(len(MAGIC) + 16)
    if not header.startswith(MAGIC) or len(header) != len(MAGIC) + 16:
        raise BackupIntegrityError("Not a DocuMind backup archive")
    aead = AESGCM(_key(passphrase, header[len(MAGIC) :]))
    index = 0
    pending: tuple[bytes, bytes] | None = None
    while True:
        head = source.read(16)
        if not head:
            break
        if len(head) != 16:
            raise BackupIntegrityError("Truncated archive")
        (length,) = struct.unpack(">I", head[12:])
        body = source.read(length)
        if len(body) != length:
            raise BackupIntegrityError("Truncated archive")
        if pending is not None:
            target.write(_open(aead, *pending, index - 1, last=False))
        pending = (head[:12], body)
        index += 1
    if pending is None:
        raise BackupIntegrityError("Empty archive")
    target.write(_open(aead, *pending, index - 1, last=True))


def _open(aead: AESGCM, nonce: bytes, body: bytes, index: int, *, last: bool) -> bytes:
    try:
        return aead.decrypt(nonce, body, _aad(index, last))
    except InvalidTag as exc:
        raise BackupIntegrityError("Wrong passphrase, or the archive was altered") from exc


def passphrase_from(path: str) -> bytes:
    data = Path(path).read_bytes().strip()
    if not data:
        raise ValueError("The passphrase file is empty")
    return data
