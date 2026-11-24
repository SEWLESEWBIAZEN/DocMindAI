"""Infrastructure profile configuration (ADR 0021).

A profile version's configuration is plain, non-secret data: secrets are
referenced by credential id (ADR 0022). It is immutable once created, and its
canonical JSON form is hashed so tampering with a stored version is detected.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast

from documind.domain.billing.plans import Entitlements

DatabaseMode = Literal["shared", "dedicated", "external"]
StorageProviderKey = Literal["documind", "aws_s3", "s3_compatible", "minio", "azure_blob"]
ProcessingMode = Literal["documind", "customer", "hybrid"]
SslMode = Literal["disable", "require", "verify-ca", "verify-full"]

_HOST = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_$-]{0,62}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$|^us-east-1$")
_PREFIX = re.compile(r"^[A-Za-z0-9!_.*'()/-]{0,200}$")
_ACCESS_KEY = re.compile(r"^[A-Za-z0-9/+=_-]{3,128}$")


@dataclass(frozen=True, slots=True)
class StorageProviderSpec:
    key: str
    name: str
    status: Literal["available", "planned"]
    description: str


STORAGE_PROVIDERS: MappingProxyType[str, StorageProviderSpec] = MappingProxyType(
    {
        "documind": StorageProviderSpec(
            "documind", "DocuMind-managed", "available", "Stored in DocuMind's private bucket."
        ),
        "aws_s3": StorageProviderSpec(
            "aws_s3", "Amazon S3", "available", "Your own bucket in your AWS account."
        ),
        "s3_compatible": StorageProviderSpec(
            "s3_compatible",
            "S3-compatible",
            "available",
            "Any S3-compatible service (Wasabi, Cloudflare R2, Ceph, ...) over HTTPS.",
        ),
        "minio": StorageProviderSpec(
            "minio", "MinIO", "available", "Your own MinIO, for example on-premises."
        ),
        "azure_blob": StorageProviderSpec(
            "azure_blob",
            "Azure Blob Storage",
            "planned",
            "Planned (MI-6). Not selectable until the adapter ships.",
        ),
    }
)


class ProfileError(ValueError):
    """Invalid configuration. The message is safe to show to users."""


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    mode: DatabaseMode = "shared"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    sslmode: SslMode | None = None
    password_credential_id: uuid.UUID | None = None
    #: External only: the customer allows DocuMind to create and migrate the schema.
    manage_schema: bool = False

    def location(self) -> tuple[Any, ...]:
        return (self.mode, self.host, self.port, self.database)


@dataclass(frozen=True, slots=True)
class StorageConfig:
    provider: StorageProviderKey = "documind"
    bucket: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    prefix: str = ""
    force_path_style: bool = False
    access_key_id: str | None = None
    secret_credential_id: uuid.UUID | None = None

    def location(self) -> tuple[Any, ...]:
        return (self.provider, self.endpoint_url, self.region, self.bucket, self.prefix)


@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    mode: ProcessingMode = "documind"


@dataclass(frozen=True, slots=True)
class InfrastructureConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)

    def to_dict(self) -> dict[str, Any]:
        def clean(value: Any) -> Any:
            if isinstance(value, uuid.UUID):
                return str(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value

        return clean(asdict(self))  # type: ignore[no-any-return]

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    @property
    def is_platform_default(self) -> bool:
        return (
            self.database.mode == "shared"
            and self.storage.provider == "documind"
            and self.processing.mode == "documind"
        )


PLATFORM_DEFAULT = InfrastructureConfig()


def _uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ProfileError("Invalid credential reference.") from exc


def _opt_str(raw: dict[str, Any], key: str, pattern: re.Pattern[str], label: str) -> str | None:
    value = raw.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise ProfileError(f"{label} isn't valid.")
    return value


def parse_config(raw: dict[str, Any]) -> InfrastructureConfig:
    """Validate the shape of a configuration (network checks happen at validation)."""
    db_raw = dict(raw.get("database") or {})
    st_raw = dict(raw.get("storage") or {})
    pr_raw = dict(raw.get("processing") or {})

    mode = db_raw.get("mode", "shared")
    if mode not in ("shared", "dedicated", "external"):
        raise ProfileError("Database mode must be shared, dedicated or external.")
    if mode == "external":
        port = db_raw.get("port") or 5432
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProfileError("Database port isn't valid.")
        sslmode = db_raw.get("sslmode") or "verify-full"
        if sslmode not in ("disable", "require", "verify-ca", "verify-full"):
            raise ProfileError("TLS mode isn't valid.")
        host = _opt_str(db_raw, "host", _HOST, "Database host")
        database = _opt_str(db_raw, "database", _IDENT, "Database name")
        username = _opt_str(db_raw, "username", _IDENT, "Database user")
        if not (host and database and username):
            raise ProfileError("An external database needs a host, database name and user.")
        db = DatabaseConfig(
            mode="external",
            host=host.lower(),
            port=port,
            database=database,
            username=username,
            sslmode=cast(SslMode, sslmode),
            password_credential_id=_uuid(db_raw.get("password_credential_id")),
            manage_schema=bool(db_raw.get("manage_schema", False)),
        )
    elif mode == "dedicated":
        # Host, database and user are assigned by provisioning (DocuMind-managed).
        db = DatabaseConfig(
            mode="dedicated",
            host=_opt_str(db_raw, "host", _HOST, "Database host"),
            port=db_raw.get("port"),
            database=_opt_str(db_raw, "database", _IDENT, "Database name"),
            username=_opt_str(db_raw, "username", _IDENT, "Database user"),
            sslmode=db_raw.get("sslmode"),
            password_credential_id=_uuid(db_raw.get("password_credential_id")),
            manage_schema=True,
        )
    else:
        db = DatabaseConfig()

    provider = st_raw.get("provider", "documind")
    spec = STORAGE_PROVIDERS.get(str(provider))
    if spec is None:
        raise ProfileError("Unknown storage provider.")
    if spec.status != "available":
        raise ProfileError(f"{spec.name} isn't available yet.")
    if provider == "documind":
        storage = StorageConfig()
    else:
        bucket = _opt_str(st_raw, "bucket", _BUCKET, "Bucket name")
        if not bucket:
            raise ProfileError("A bucket name is required.")
        prefix = st_raw.get("prefix") or ""
        if not isinstance(prefix, str) or not _PREFIX.match(prefix) or ".." in prefix:
            raise ProfileError("Key prefix isn't valid.")
        prefix = prefix.strip("/")
        region = _opt_str(st_raw, "region", _REGION, "Region")
        endpoint = st_raw.get("endpoint_url") or None
        if provider == "aws_s3":
            if endpoint is not None:
                raise ProfileError(
                    "Amazon S3 uses the regional AWS endpoint; leave the endpoint empty."
                )
            if region is None:
                raise ProfileError("Amazon S3 needs a region.")
        else:
            if not isinstance(endpoint, str) or not endpoint:
                raise ProfileError("An endpoint URL is required.")
        storage = StorageConfig(
            provider=provider,
            bucket=bucket,
            region=region or "us-east-1",
            endpoint_url=endpoint,
            prefix=prefix,
            force_path_style=bool(st_raw.get("force_path_style", provider == "minio")),
            access_key_id=_opt_str(st_raw, "access_key_id", _ACCESS_KEY, "Access key ID"),
            secret_credential_id=_uuid(st_raw.get("secret_credential_id")),
        )
        if storage.access_key_id is None:
            raise ProfileError("An access key ID is required.")

    pmode = pr_raw.get("mode", "documind")
    if pmode not in ("documind", "customer", "hybrid"):
        raise ProfileError("Processing mode must be documind, customer or hybrid.")
    return InfrastructureConfig(db, storage, ProcessingConfig(pmode))


def from_stored(raw: dict[str, Any]) -> InfrastructureConfig:
    """Rebuild a stored configuration exactly (no defaults applied)."""
    db = dict(raw["database"])
    st = dict(raw["storage"])
    db["password_credential_id"] = _uuid(db.get("password_credential_id"))
    st["secret_credential_id"] = _uuid(st.get("secret_credential_id"))
    return InfrastructureConfig(
        DatabaseConfig(**db), StorageConfig(**st), ProcessingConfig(**raw["processing"])
    )


def check_entitlements(config: InfrastructureConfig, ents: Entitlements) -> None:
    if config.storage.provider != "documind" and not ents.custom_storage:
        raise ProfileError("Your plan doesn't include your own storage.")
    if config.database.mode == "dedicated" and not ents.dedicated_database:
        raise ProfileError("Your plan doesn't include a dedicated database.")
    if config.database.mode == "external" and not ents.external_database:
        raise ProfileError("Your plan doesn't include connecting your own database.")
    if config.processing.mode != "documind" and not ents.customer_workers:
        raise ProfileError("Your plan doesn't include customer-hosted processing.")


def requires_migration(current: InfrastructureConfig, target: InfrastructureConfig) -> list[str]:
    """What data would have to move (empty: the switch needs no data migration)."""
    moves = []
    if current.database.location() != target.database.location():
        moves.append("database")
    if current.storage.location() != target.storage.location():
        moves.append("storage")
    return moves
