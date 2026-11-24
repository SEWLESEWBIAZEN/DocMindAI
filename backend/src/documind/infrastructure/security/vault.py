"""Envelope encryption for customer credentials (ADR 0022).

Each secret gets its own random 256-bit data key (DEK). The secret is sealed
with AES-256-GCM under the DEK; the DEK is wrapped with a key-encryption key
(KEK) from the key ring. Associated data binds both layers to the credential's
id, workspace and kind, so a row copied elsewhere fails to decrypt.

Rotation: put a new key first in the ring and rewrap. Rewrapping touches only
the wrapped DEKs, never the secret ciphertext.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12


class VaultError(Exception):
    """Decryption failed (wrong key, tampered row, or bound to another context).

    The message never contains secret material.
    """


class KeyEncryptionProvider(Protocol):
    """Wraps and unwraps data keys. Local key ring today; KMS/Vault later."""

    @property
    def primary_id(self) -> str: ...

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]: ...

    def unwrap(self, kek_id: str, wrapped: bytes, aad: bytes) -> bytes: ...

    def has(self, kek_id: str) -> bool: ...


class LocalKeyRing:
    def __init__(self, keys: list[tuple[str, bytes]]) -> None:
        if not keys:
            raise ValueError("The key ring needs at least one key")
        for key_id, key in keys:
            if len(key) != 32:
                raise ValueError(f"Key '{key_id}' must be 32 bytes")
        self._order = [k for k, _ in keys]
        self._keys = {k: AESGCM(v) for k, v in keys}

    @property
    def primary_id(self) -> str:
        return self._order[0]

    def has(self, kek_id: str) -> bool:
        return kek_id in self._keys

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(_NONCE)
        return self.primary_id, nonce + self._keys[self.primary_id].encrypt(nonce, dek, aad)

    def unwrap(self, kek_id: str, wrapped: bytes, aad: bytes) -> bytes:
        aead = self._keys.get(kek_id)
        if aead is None:
            raise VaultError(f"Key '{kek_id}' is not in the key ring")
        try:
            return aead.decrypt(wrapped[:_NONCE], wrapped[_NONCE:], aad)
        except (InvalidTag, ValueError) as exc:
            raise VaultError("Data key could not be unwrapped") from exc


@dataclass(frozen=True, slots=True)
class Sealed:
    ciphertext: str  # base64url(nonce + AES-GCM(secret))
    wrapped_dek: str  # base64url(nonce + AES-GCM(dek))
    kek_id: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode())


class EnvelopeCipher:
    def __init__(self, keys: KeyEncryptionProvider) -> None:
        self._keys = keys

    @property
    def primary_id(self) -> str:
        return self._keys.primary_id

    def seal(self, secret: str, aad: str) -> Sealed:
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(_NONCE)
        body = nonce + AESGCM(dek).encrypt(nonce, secret.encode(), aad.encode())
        kek_id, wrapped = self._keys.wrap(dek, aad.encode())
        return Sealed(_b64(body), _b64(wrapped), kek_id)

    def open(self, sealed: Sealed, aad: str) -> str:
        try:
            dek = self._keys.unwrap(sealed.kek_id, _unb64(sealed.wrapped_dek), aad.encode())
            raw = _unb64(sealed.ciphertext)
            return AESGCM(dek).decrypt(raw[:_NONCE], raw[_NONCE:], aad.encode()).decode()
        except VaultError:
            raise
        except (InvalidTag, ValueError) as exc:
            raise VaultError("Credential could not be decrypted") from exc

    def rewrap(self, sealed: Sealed, aad: str) -> Sealed:
        """Re-wrap the data key with the primary KEK (key rotation)."""
        if sealed.kek_id == self._keys.primary_id:
            return sealed
        dek = self._keys.unwrap(sealed.kek_id, _unb64(sealed.wrapped_dek), aad.encode())
        kek_id, wrapped = self._keys.wrap(dek, aad.encode())
        return Sealed(sealed.ciphertext, _b64(wrapped), kek_id)
