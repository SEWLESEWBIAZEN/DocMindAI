"""Value rules for identity data (pure functions)."""

from __future__ import annotations

from documind.domain.errors import ValidationFailedError

PASSWORD_MIN_LENGTH = 10
# Bounds hashing cost; long passphrases are still allowed.
PASSWORD_MAX_LENGTH = 128
DISPLAY_NAME_MAX_LENGTH = 100


def normalize_email(email: str) -> str:
    """Canonical form used for storage and lookup (case-insensitive, trimmed)."""
    return email.strip().lower()


def validate_password(password: str, *, email: str | None = None) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationFailedError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValidationFailedError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    if not password.strip():
        raise ValidationFailedError("Password can't be only whitespace.")
    if email and password.strip().lower() == normalize_email(email):
        raise ValidationFailedError("Password can't be your email address.")


def clean_display_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValidationFailedError("Name can't be empty.")
    if len(cleaned) > DISPLAY_NAME_MAX_LENGTH:
        raise ValidationFailedError(f"Name must be at most {DISPLAY_NAME_MAX_LENGTH} characters.")
    return cleaned
