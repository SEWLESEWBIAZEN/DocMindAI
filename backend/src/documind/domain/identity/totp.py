"""Time-based one-time passwords (RFC 6238, HMAC-SHA1, 30 s, 6 digits).

SHA-1 is what every mainstream authenticator app supports for TOTP; HOTP's
security does not rely on SHA-1 collision resistance (RFC 4226 §B).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

PERIOD_SECONDS = 30
DIGITS = 6
# Accept the previous and next time step to tolerate clock drift.
DRIFT_STEPS = 1


def generate_secret() -> str:
    """160-bit random secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode(secret_b32: str) -> bytes:
    cleaned = secret_b32.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def time_step(unix_seconds: float) -> int:
    return int(unix_seconds // PERIOD_SECONDS)


def code_at_step(secret_b32: str, step: int, digits: int = DIGITS) -> str:
    digest = hmac.new(_decode(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify(
    secret_b32: str, code: str, unix_seconds: float, *, last_used_step: int | None
) -> int | None:
    """Return the matched time step, or None.

    Steps at or before ``last_used_step`` are refused, so a code (or an older
    one) can never be replayed.
    """
    normalized = "".join(code.split())
    if len(normalized) != DIGITS or not normalized.isdigit():
        return None
    current = time_step(unix_seconds)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(code_at_step(secret_b32, step), normalized):
            return step
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """otpauth:// URI for QR codes; the secret is also shown for manual entry."""
    label = quote(f"{issuer}:{account}", safe="")
    params = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{params}"


# --- Recovery codes -----------------------------------------------------------

RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i confusion


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Codes like ``k7mq-3xnr-pw9h`` (12 symbols ≈ 59 bits each)."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery_code(code: str, pepper: bytes) -> str:
    """Keyed hash: a leaked database alone doesn't allow offline guessing."""
    return hmac.new(pepper, normalize_recovery_code(code).encode(), hashlib.sha256).hexdigest()
