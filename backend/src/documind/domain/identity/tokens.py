"""Opaque, single-use secrets (refresh, verification, reset, invitation tokens).

Only the SHA-256 digest is stored, so a database leak does not leak usable
tokens. 256 bits of randomness makes a slow hash unnecessary.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
