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

    # Browser-reachable endpoint used only to sign download URLs (the API may
    # reach storage on an internal hostname). Defaults to S3_ENDPOINT_URL.
    s3_public_endpoint_url: str | None = None

    # --- Documents ---------------------------------------------------------
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    signed_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    # "none" or "clamav" (clamd over TCP).
    malware_scanner: Literal["none", "clamav"] = "none"
    clamav_host: str = "clamav"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Health checks -----------------------------------------------------
    health_check_timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    # --- Public URLs -------------------------------------------------------
    # Base URL of the web app; used to build links in emails.
    public_app_url: str = "http://localhost:5173"

    # --- Authentication ----------------------------------------------------
    jwt_secret_key: SecretStr = Field(min_length=32)
    jwt_issuer: str = "documind"
    jwt_audience: str = "documind-api"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)
    refresh_cookie_name: str = "documind_refresh"
    # Secure cookies are required in production; browsers also accept them on
    # http://localhost, so this stays on in development too.
    auth_cookie_secure: bool = True
    email_verification_ttl_hours: int = Field(default=24, ge=1, le=168)
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=240)
    invitation_ttl_days: int = Field(default=7, ge=1, le=30)
    # Argon2id cost (RFC 9106 second recommended option by default).
    password_hash_time_cost: int = Field(default=3, ge=1, le=10)
    password_hash_memory_kib: int = Field(default=65536, ge=8, le=1048576)
    password_hash_parallelism: int = Field(default=4, ge=1, le=16)

    # --- Rate limits (requests per window, per client IP unless noted) ------
    rate_limit_enabled: bool = True
    rate_limit_login_per_ip: int = Field(default=20, ge=1)
    rate_limit_login_per_email: int = Field(default=10, ge=1)
    rate_limit_login_window_seconds: int = Field(default=300, ge=1)
    rate_limit_auth_per_ip: int = Field(default=10, ge=1)
    rate_limit_auth_window_seconds: int = Field(default=600, ge=1)
    rate_limit_refresh_per_ip: int = Field(default=60, ge=1)
    rate_limit_refresh_window_seconds: int = Field(default=60, ge=1)

    # --- Email (SMTP) ------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)
    email_from: str = "DocuMind AI <no-reply@documind.local>"

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
            "JWT_SECRET_KEY": self.jwt_secret_key,
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
        if not self.auth_cookie_secure:
            raise ValueError("AUTH_COOKIE_SECURE must be true in production")
        if not self.public_app_url.startswith("https://"):
            raise ValueError("PUBLIC_APP_URL must use https in production")
        if not (self.smtp_starttls or self.smtp_use_ssl):
            raise ValueError("SMTP must use STARTTLS or SSL in production")
        return self

    @model_validator(mode="after")
    def _validate_smtp(self) -> Settings:
        if self.smtp_starttls and self.smtp_use_ssl:
            raise ValueError("Choose either SMTP_STARTTLS or SMTP_USE_SSL, not both")
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
