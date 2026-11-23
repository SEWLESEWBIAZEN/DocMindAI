"""AES-256-GCM encryption for small secrets stored in the database (e.g. TOTP)."""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = "v1"


class SecretDecryptionError(Exception):
    pass


class SecretBox:
    """Encrypts with a random 96-bit nonce. ``context`` (e.g. the user ID) is bound
    as associated data, so a ciphertext copied to another row won't decrypt."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("SecretBox requires a 32-byte key")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{_VERSION}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, token: str, *, context: str) -> str:
        version, _, payload = token.partition(":")
        if version != _VERSION:
            raise SecretDecryptionError("Unknown ciphertext version")
        try:
            raw = base64.urlsafe_b64decode(payload)
            return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode()
        except (InvalidTag, ValueError) as exc:
            raise SecretDecryptionError(
                "Ciphertext is invalid or bound to another context"
            ) from exc
