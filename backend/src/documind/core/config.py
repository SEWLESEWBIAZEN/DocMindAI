"""Application configuration.

All runtime configuration comes from environment variables (optionally loaded
from a ``.env`` file in development). Nothing secret has a default value, so a
misconfigured deployment fails fast at startup instead of silently running with
insecure defaults.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]

# Values that appear in `.env.example`. They must never reach production.
_INSECURE_SECRET_MARKERS = ("change-me", "dev-only", "insecure")


class Settings(BaseSettings):
    """Typed, validated application settings."""

    model_config = SettingsConfigDict(
        # The repository-root .env is shared with Docker Compose; an optional
        # backend/.env overrides it for native (non-container) development.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "DocuMind AI"
    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"
    # None means "enabled everywhere except production".
    api_docs_enabled: bool | None = None
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- PostgreSQL --------------------------------------------------------
    database_url: SecretStr
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    database_echo: bool = False

    # --- Redis -------------------------------------------------------------
    redis_url: SecretStr
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- S3-compatible object storage --------------------------------------
    # Leave the endpoint unset to use AWS S3; set it for MinIO or other providers.
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr
    s3_secret_access_key: SecretStr
    s3_bucket: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]+[a-z0-9]$")
    s3_force_path_style: bool = True
    s3_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    s3_read_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Health checks -----------------------------------------------------
    health_check_timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        scheme = urlsplit(value.get_secret_value()).scheme
        if scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the 'postgresql+asyncpg' scheme")
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: SecretStr) -> SecretStr:
        scheme = urlsplit(value.get_secret_value()).scheme
        if scheme not in {"redis", "rediss"}:
            raise ValueError("REDIS_URL must use the 'redis' or 'rediss' scheme")
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.environment is not Environment.PRODUCTION:
            return self
        secrets = {
            "DATABASE_URL": self.database_url,
            "REDIS_URL": self.redis_url,
            "S3_ACCESS_KEY_ID": self.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
        }
        insecure = [
            name
            for name, secret in secrets.items()
            if any(
                marker in secret.get_secret_value().lower() for marker in _INSECURE_SECRET_MARKERS
            )
        ]
        if insecure:
            raise ValueError(
                "Development placeholder credentials are not allowed in production: "
                + ", ".join(sorted(insecure))
            )
        if "*" in self.cors_allowed_origins:
            raise ValueError("Wildcard CORS origins are not allowed in production")
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.environment is not Environment.PRODUCTION

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (loaded once)."""
    return Settings()  # values come from the environment
