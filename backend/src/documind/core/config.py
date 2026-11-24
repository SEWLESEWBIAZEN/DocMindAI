"""Application configuration.

All runtime configuration comes from environment variables (optionally loaded
from a ``.env`` file in development). Nothing secret has a default value, so a
misconfigured deployment fails fast at startup instead of silently running with
insecure defaults.
"""

from __future__ import annotations

import base64
import binascii
import re
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from documind.domain.processing.vectors import VECTOR_DIMENSIONS


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

    # --- Document processing (Celery workers) ------------------------------
    # Defaults to REDIS_URL when unset.
    celery_broker_url: SecretStr | None = None
    processing_max_attempts: int = Field(default=5, ge=1, le=20)
    processing_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    # A running job whose lease expires (worker crashed) is re-queued.
    processing_lease_seconds: int = Field(default=1800, ge=60, le=86400)
    # Queued jobs not picked up within this time are re-dispatched by the sweeper.
    processing_dispatch_grace_seconds: int = Field(default=60, ge=5, le=3600)
    processing_sweep_interval_seconds: int = Field(default=30, ge=5, le=3600)
    max_document_pages: int = Field(default=2000, ge=1, le=100_000)
    chunk_max_tokens: int = Field(default=500, ge=50, le=4000)
    chunk_overlap_tokens: int = Field(default=60, ge=0, le=1000)
    # Keep each chunk on a single page so citations point at exact pages.
    chunk_respect_pages: bool = True
    ocr_enabled: bool = True
    ocr_languages: str = Field(default="eng", pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$")
    ocr_dpi: int = Field(default=300, ge=72, le=600)

    # --- Embeddings --------------------------------------------------------
    # "openai" in production; "local" is a deterministic, network-free
    # development/test provider and is rejected in production.
    embedding_provider: Literal["openai", "local"] = "openai"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    embedding_model: str = "text-embedding-3-small"
    # Must match the vector column created by the migrations.
    embedding_dimensions: int = 1536
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)
    embedding_max_retries: int = Field(default=5, ge=0, le=10)
    embedding_timeout_seconds: float = Field(default=60.0, gt=0)

    # --- Answer generation (LLM) --------------------------------------------
    # "local" is an offline extractive stand-in for development and tests; it
    # is rejected in production. The OpenAI provider shares OPENAI_API_KEY.
    llm_provider: Literal["openai", "local"] = "openai"
    llm_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=100)
    # None = provider default. Reasoning models reject custom temperatures.
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    llm_max_output_tokens: int = Field(default=1200, ge=64, le=32_000)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=10)

    # --- Retrieval ------------------------------------------------------------
    retrieval_top_k: int = Field(default=8, ge=1, le=50)
    # Candidates fetched from each retriever before fusion.
    retrieval_candidates: int = Field(default=40, ge=1, le=500)
    # Minimum cosine similarity for a vector hit (0 disables the threshold).
    retrieval_min_score: float = Field(default=0.2, ge=0, le=1)
    retrieval_hybrid: bool = True
    retrieval_ef_search: int = Field(default=100, ge=10, le=1000)
    rag_context_max_tokens: int = Field(default=6000, ge=500, le=100_000)
    rate_limit_ai_per_user: int = Field(default=30, ge=1)
    rate_limit_ai_window_seconds: int = Field(default=60, ge=1)

    # --- Voice (ADR 0011) ----------------------------------------------------
    # "openai": server speech-to-text and text-to-speech (uses OPENAI_API_KEY).
    # "none": only the browser's built-in speech features are available.
    speech_provider: Literal["openai", "none"] = "none"
    stt_model: str = Field(default="gpt-4o-mini-transcribe", min_length=1, max_length=100)
    tts_model: str = Field(default="gpt-4o-mini-tts", min_length=1, max_length=100)
    tts_voice: str = Field(default="alloy", min_length=1, max_length=50)
    speech_timeout_seconds: float = Field(default=60.0, gt=0)
    speech_max_audio_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    speech_max_text_chars: int = Field(default=4096, ge=100, le=4096)
    rate_limit_voice_per_user: int = Field(default=120, ge=1)
    rate_limit_voice_window_seconds: int = Field(default=60, ge=1)
    # Requests that make DocuMind connect out to a customer-chosen host (key and
    # infrastructure validation): per workspace, to stop them being used as a probe.
    rate_limit_outbound_checks_per_workspace: int = Field(default=20, ge=1)
    rate_limit_outbound_checks_window_seconds: int = Field(default=3600, ge=1)

    # --- Figure descriptions (ADR 0011) --------------------------------------
    # Describe images in PDFs with a vision model during processing, so figures
    # and charts become searchable, citable and readable aloud. Costs one model
    # call per figure. Uses the LLM provider (the local one gives placeholders).
    figure_descriptions_enabled: bool = False
    vision_model: str | None = Field(default=None, max_length=100)
    figure_max_per_document: int = Field(default=20, ge=0, le=500)
    figure_min_size_points: float = Field(default=72.0, ge=10)

    # --- Conversations -------------------------------------------------------
    # Earlier turns sent to the model (most recent first, within the budget).
    conversation_history_turns: int = Field(default=6, ge=0, le=50)
    conversation_history_max_tokens: int = Field(default=2000, ge=0, le=50_000)
    # Rewrite follow-ups ("what about the second one?") into standalone queries.
    conversation_condense_followups: bool = True
    # SSE keep-alive comment interval while the model is thinking.
    chat_stream_heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    # An answer still "streaming" after this long is marked failed (crash recovery).
    chat_stream_stale_seconds: int = Field(default=300, ge=30)
    # How often a running stream checks for a cancel request.
    chat_cancel_poll_seconds: float = Field(default=0.5, gt=0, le=10)

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

    # --- Two-factor authentication ----------------------------------------
    # 32 random bytes, base64url-encoded. Encrypts TOTP secrets at rest and
    # keys the recovery-code hashes. Generate with:
    #   python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    mfa_encryption_key: SecretStr
    mfa_issuer: str = "DocuMind"
    # Credential vault (ADR 0022): "id:base64key,id:base64key". The first key wraps
    # new data keys; every key can unwrap. Rotate by prepending a new key, running
    # `documind-manage credentials rewrap`, then removing the old one.
    credential_encryption_keys: SecretStr | None = None
    # How long API and worker processes cache a workspace's plan and AI-provider
    # settings. Changes made in another process apply within this time.
    tenant_settings_cache_seconds: float = Field(default=10.0, ge=0, le=300)

    # Infrastructure independence (ADR 0021).
    # Hosts that may resolve to private addresses (on-premises databases and storage).
    infrastructure_allowed_private_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    # DocuMind-managed server for dedicated databases: a role allowed to CREATE DATABASE
    # and CREATE ROLE. Unset: dedicated databases can't be provisioned here.
    dedicated_database_admin_url: SecretStr | None = None
    # Host and port that workers and the API use to reach provisioned databases.
    dedicated_database_host: str | None = None
    dedicated_database_port: int = Field(default=5432, ge=1, le=65535)
    dedicated_database_sslmode: Literal["disable", "require", "verify-ca", "verify-full"] = (
        "verify-full"
    )
    dataplane_pool_size: int = Field(default=5, ge=1, le=50)
    dataplane_max_open: int = Field(default=20, ge=1, le=500)
    # How long a successful 2FA check authorizes sensitive actions (e.g. delete).
    mfa_step_up_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    mfa_setup_ttl_minutes: int = Field(default=15, ge=1, le=60)
    rate_limit_mfa_per_user: int = Field(default=5, ge=1)
    rate_limit_mfa_window_seconds: int = Field(default=300, ge=1)

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

    @field_validator("infrastructure_allowed_private_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        scheme = urlsplit(value.get_secret_value()).scheme
        if scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the 'postgresql+asyncpg' scheme")
        return value

    @field_validator("mfa_encryption_key")
    @classmethod
    def _validate_mfa_key(cls, value: SecretStr) -> SecretStr:
        if len(mfa_key_bytes(value)) != 32:
            raise ValueError("MFA_ENCRYPTION_KEY must be 32 bytes, base64url-encoded")
        return value

    @field_validator("credential_encryption_keys")
    @classmethod
    def _validate_credential_keys(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            credential_key_ring(value)  # raises ValueError when malformed
        return value

    @field_validator(
        "openai_api_key",
        "celery_broker_url",
        "smtp_password",
        "credential_encryption_keys",
        "dedicated_database_admin_url",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes unset variables as empty strings.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("embedding_dimensions")
    @classmethod
    def _validate_dimensions(cls, value: int) -> int:
        if value != VECTOR_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {VECTOR_DIMENSIONS} (the vector column size); "
                "changing it requires a migration"
            )
        return value

    @model_validator(mode="after")
    def _validate_embeddings(self) -> Settings:
        if self.embedding_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.speech_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when SPEECH_PROVIDER=openai")
        if self.chunk_overlap_tokens >= self.chunk_max_tokens // 2:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be less than half of CHUNK_MAX_TOKENS")
        return self

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
        decoded_mfa_key = mfa_key_bytes(self.mfa_encryption_key).lower()
        if any(marker.encode() in decoded_mfa_key for marker in _INSECURE_SECRET_MARKERS):
            insecure.append("MFA_ENCRYPTION_KEY")
        if self.credential_encryption_keys is None:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEYS is required in production (ADR 0022)")
        for key_id, key in credential_key_ring(self.credential_encryption_keys):
            if any(marker.encode() in key.lower() for marker in _INSECURE_SECRET_MARKERS):
                insecure.append(f"CREDENTIAL_ENCRYPTION_KEYS ({key_id})")
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
        if self.embedding_provider != "openai":
            raise ValueError("EMBEDDING_PROVIDER=local is for development only")
        if self.llm_provider != "openai":
            raise ValueError("LLM_PROVIDER=local is for development only")
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
    def broker_url(self) -> str:
        return (self.celery_broker_url or self.redis_url).get_secret_value()

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


def mfa_key_bytes(value: SecretStr) -> bytes:
    raw = value.get_secret_value().strip()
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error):
        return b""


_KEY_ID = re.compile(r"^[a-z0-9_-]{1,16}$")


def credential_key_ring(value: SecretStr) -> list[tuple[str, bytes]]:
    """Parse ``id:base64url,id:base64url``; first entry is the primary key."""
    ring: list[tuple[str, bytes]] = []
    for item in value.get_secret_value().split(","):
        key_id, sep, encoded = item.strip().partition(":")
        if not sep or not _KEY_ID.match(key_id):
            raise ValueError("CREDENTIAL_ENCRYPTION_KEYS entries must look like 'k1:<base64url>'")
        key = mfa_key_bytes(SecretStr(encoded))
        if len(key) != 32:
            raise ValueError(f"CREDENTIAL_ENCRYPTION_KEYS key '{key_id}' must be 32 bytes")
        if key_id in {existing for existing, _ in ring}:
            raise ValueError(f"CREDENTIAL_ENCRYPTION_KEYS has duplicate id '{key_id}'")
        ring.append((key_id, key))
    if not ring:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEYS is empty")
    return ring


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (loaded once)."""
    return Settings()  # values come from the environment
