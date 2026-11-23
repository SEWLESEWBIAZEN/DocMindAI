"""Shared test fixtures."""

from collections.abc import Callable
from typing import Any

import pytest

from documind.core.config import Settings

SettingsFactory = Callable[..., Settings]

_BASE: dict[str, Any] = {
    "environment": "test",
    "log_level": "WARNING",
    "log_format": "console",
    "database_url": "postgresql+asyncpg://user:pass@127.0.0.1:1/db",
    "redis_url": "redis://127.0.0.1:1/0",
    "s3_endpoint_url": "http://127.0.0.1:1",
    "s3_access_key_id": "test-access-key",
    "s3_secret_access_key": "test-secret-key",
    "s3_bucket": "documind-test",
    "jwt_secret_key": "test-jwt-secret-key-that-is-long-enough-0123456789",
    "auth_cookie_secure": False,
    "embedding_provider": "local",
    "mfa_encryption_key": "dGVzdC1tZmEta2V5LTAxMjM0NTY3ODlhYmNkZWZnaGk=",
    "password_hash_time_cost": 1,
    "password_hash_memory_kib": 8,
    "password_hash_parallelism": 1,
}


# The minimum overrides that make settings valid for ENVIRONMENT=production.
PRODUCTION: dict[str, Any] = {
    "environment": "production",
    "auth_cookie_secure": True,
    "public_app_url": "https://app.example.com",
    "smtp_starttls": True,
    "embedding_provider": "openai",
    "openai_api_key": "sk-production-test-key",
}


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Build Settings from explicit values only; never read .env or the process env."""

    def _make(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **{**_BASE, **overrides})

    return _make


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings()
