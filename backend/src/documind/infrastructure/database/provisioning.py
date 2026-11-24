"""Dedicated database provisioning and data-plane validation (MI-3, MI-4)."""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from documind.core.config import Settings
from documind.domain.infrastructure.profiles import DatabaseConfig
from documind.infrastructure.database.dataplane_runtime import (
    create_dataplane_engine,
    head_revision,
    migrate,
    schema_revision,
)
from documind.infrastructure.network.ssrf import BlockedHostError, check_host

logger = logging.getLogger(__name__)

MIN_SERVER_VERSION = 140000


@dataclass(frozen=True, slots=True)
class DatabaseCheck:
    ok: bool
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class ProvisioningUnavailableError(RuntimeError):
    pass


async def validate_database(
    settings: Settings, config: DatabaseConfig, password: str, *, check_network: bool = True
) -> DatabaseCheck:
    """Connect and check version, privileges, pgvector and the schema revision.

    Runs data-plane migrations only for dedicated databases or when the customer
    allowed DocuMind to manage the schema.
    """
    if config.mode == "shared":
        return DatabaseCheck(True, details={"mode": "shared"})
    if check_network and config.host:
        try:
            await check_host(
                config.host,
                config.port or 5432,
                allowed=frozenset(settings.infrastructure_allowed_private_hosts),
            )
        except BlockedHostError as exc:
            return DatabaseCheck(False, "host_not_allowed", str(exc))
    if settings.is_production and config.mode == "external" and config.sslmode != "verify-full":
        return DatabaseCheck(
            False, "tls_required", "Production connections must use TLS with verify-full."
        )
    engine = create_dataplane_engine(settings, config, password, pool_size=1)
    details: dict[str, Any] = {}
    try:
        async with engine.connect() as conn:
            version = int((await conn.execute(text("SHOW server_version_num"))).scalar_one())
            details["server_version_num"] = version
            if version < MIN_SERVER_VERSION:
                return DatabaseCheck(
                    False, "server_too_old", "PostgreSQL 14 or later is required.", details
                )
            superuser = (
                await conn.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()
            if superuser:
                return DatabaseCheck(
                    False,
                    "superuser_not_allowed",
                    "Use a dedicated, non-superuser role for DocuMind.",
                    details,
                )
            installed = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
            available = (
                await conn.execute(
                    text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
                )
            ).scalar_one_or_none()
            details["pgvector"] = installed or ("available" if available else None)
            if installed is None:
                return DatabaseCheck(
                    False,
                    "pgvector_missing",
                    "Install the pgvector extension in this database (CREATE EXTENSION vector).",
                    details,
                )
        revision = await schema_revision(engine)
        head = head_revision()
        if revision != head:
            if config.mode == "dedicated" or config.manage_schema:
                revision = await migrate(engine)
            else:
                return DatabaseCheck(
                    False,
                    "schema_not_current",
                    f"The DocuMind schema is at {revision or 'none'}, expected {head}. Allow "
                    "DocuMind to manage the schema, or run the data-plane migrations.",
                    details,
                )
        details["schema_revision"] = revision
        return DatabaseCheck(True, details=details)
    except (DBAPIError, OSError, TimeoutError) as exc:
        message = "The database couldn't be reached or rejected the credentials."
        if "password authentication failed" in str(exc).lower():
            message = "The database rejected the user name or password."
        logger.warning("data-plane validation failed", extra={"error": type(exc).__name__})
        return DatabaseCheck(False, "database_unreachable", message, details)
    except SQLAlchemyError:
        logger.exception("data-plane validation error")
        return DatabaseCheck(False, "database_error", "The database check failed.", details)
    finally:
        await engine.dispose()


class DedicatedProvisioner:
    """Creates one database and one login role per organization on a DocuMind server."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        return self._settings.dedicated_database_admin_url is not None

    async def provision(self, workspace_id: uuid.UUID) -> tuple[DatabaseConfig, str]:
        admin = self._settings.dedicated_database_admin_url
        if admin is None:
            raise ProvisioningUnavailableError("dedicated databases aren't configured")
        admin_url = make_url(admin.get_secret_value())
        suffix = workspace_id.hex[:20]
        name, role = f"dm_{suffix}", f"dm_{suffix}_app"
        # Identifiers and password come from hex / token_urlsafe only: safe to inline in DDL.
        password = secrets.token_urlsafe(32)
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                exists = (
                    await conn.execute(
                        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                    )
                ).scalar_one_or_none()
                if exists:
                    await conn.execute(
                        text(f"ALTER ROLE \"{role}\" WITH LOGIN PASSWORD '{password}'")
                    )
                else:
                    await conn.execute(
                        text(
                            f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                            f"PASSWORD '{password}'"
                        )
                    )
                db_exists = (
                    await conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": name}
                    )
                ).scalar_one_or_none()
                if not db_exists:
                    await conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{role}"'))
                await conn.execute(text(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC'))
        finally:
            await engine.dispose()
        # pgvector needs a privileged role; then the schema belongs to the app role.
        ext = create_async_engine(admin_url.set(database=name), isolation_level="AUTOCOMMIT")
        try:
            async with ext.connect() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        finally:
            await ext.dispose()
        config = DatabaseConfig(
            mode="dedicated",
            host=self._settings.dedicated_database_host or admin_url.host,
            port=self._settings.dedicated_database_port
            if self._settings.dedicated_database_host
            else (admin_url.port or 5432),
            database=name,
            username=role,
            sslmode=self._settings.dedicated_database_sslmode,
            manage_schema=True,
        )
        app = create_dataplane_engine(self._settings, config, password, pool_size=1)
        try:
            await migrate(app)
        finally:
            await app.dispose()
        logger.info(
            "dedicated database provisioned",
            extra={"workspace_id": str(workspace_id), "database": name},
        )
        return config, password


def with_password_credential(config: DatabaseConfig, credential_id: uuid.UUID) -> DatabaseConfig:
    return replace(config, password_credential_id=credential_id)
