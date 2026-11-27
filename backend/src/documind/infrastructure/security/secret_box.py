"""AES-256-GCM encryption for small secrets stored in the database (e.g. TOTP).

Key rotation (production readiness F4): the box holds the current key and any
previous keys. It always encrypts with the current key and writes
``v2:<key id>:<payload>``, where the key id is the first 8 hex characters of
SHA-256 of the key (it identifies the key without revealing it). It decrypts:

* ``v2`` with the key its id names;
* legacy ``v1`` (no key id) by trying the current key, then the previous ones.

``documind-manage mfa rewrap`` re-encrypts stored secrets with the current key, so
previous keys can then be retired.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_LEGACY = "v1"
_VERSION = "v2"


class SecretDecryptionError(Exception):
    pass


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


class SecretBox:
    """Encrypts with a random 96-bit nonce. ``context`` (e.g. the user ID) is bound
    as associated data, so a ciphertext copied to another row won't decrypt."""

    def __init__(self, key: bytes, previous: Sequence[bytes] = ()) -> None:
        for k in (key, *previous):
            if len(k) != 32:
                raise ValueError("SecretBox requires 32-byte keys")
        self._primary_id = key_id(key)
        self._keys: dict[str, AESGCM] = {key_id(k): AESGCM(k) for k in (*previous, key)}
        # Legacy v1 tries the current key first.
        self._order = [self._primary_id] + [
            key_id(k) for k in previous if key_id(k) != self._primary_id
        ]

    @property
    def primary_id(self) -> str:
        return self._primary_id

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(12)
        sealed = self._keys[self._primary_id].encrypt(nonce, plaintext.encode(), context.encode())
        payload = base64.urlsafe_b64encode(nonce + sealed).decode()
        return f"{_VERSION}:{self._primary_id}:{payload}"

    def decrypt(self, token: str, *, context: str) -> str:
        version, _, rest = token.partition(":")
        if version == _VERSION:
            kid, _, payload = rest.partition(":")
            if kid not in self._keys:
                raise SecretDecryptionError(f"Ciphertext key {kid} is not configured")
            candidates = [kid]
        elif version == _LEGACY:
            payload, candidates = rest, self._order
        else:
            raise SecretDecryptionError("Unknown ciphertext version")
        try:
            raw = base64.urlsafe_b64decode(payload)
        except ValueError as exc:
            raise SecretDecryptionError("Ciphertext is malformed") from exc
        for kid in candidates:
            try:
                return self._keys[kid].decrypt(raw[:12], raw[12:], context.encode()).decode()
            except (InvalidTag, ValueError):
                continue
        raise SecretDecryptionError("Ciphertext is invalid or bound to another context")

    def is_current(self, token: str) -> bool:
        """Encrypted with the current key in the current format (nothing to rewrap)."""
        version, _, rest = token.partition(":")
        return version == _VERSION and rest.partition(":")[0] == self._primary_id
