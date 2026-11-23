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
