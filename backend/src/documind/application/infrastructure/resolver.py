"""Infrastructure resolution (MI-1, ADR 0021).

``resolve(workspace_id)`` returns where the workspace's data lives: its
data-plane sessions and object storage. Workspaces without an active profile
version use the platform defaults (the shared database and the platform
bucket), so existing workspaces are unaffected.

Resolution is driven only by an authorized workspace id. Versions are
immutable and their checksum is verified on load; built adapters are cached
per version and engines are evicted LRU beyond ``DATAPLANE_MAX_OPEN``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.credentials.vault import CredentialVault
from documind.application.ports import ObjectStorageProvider
from documind.core.config import Settings
from documind.domain.errors import ServiceUnavailableError
from documind.domain.infrastructure.profiles import (
    PLATFORM_DEFAULT,
    InfrastructureConfig,
    from_stored,
)
from documind.infrastructure.database.dataplane_runtime import (
    DataPlane,
    create_dataplane_engine,
    dataplane_sessions,
)
from documind.infrastructure.database.models import (
    InfrastructureProfileVersion,
    WorkspaceInfrastructure,
)
from documind.infrastructure.network.ssrf import BlockedHostError, check_host, check_url
from documind.infrastructure.security.vault import EnvelopeCipher, VaultError
from documind.infrastructure.storage.factory import BuiltStorage, build_storage

logger = logging.getLogger(__name__)


class InfrastructureUnavailableError(ServiceUnavailableError):
    code = "infrastructure_unavailable"


@dataclass(frozen=True, slots=True)
class ResolvedInfrastructure:
    workspace_id: uuid.UUID
    version_id: uuid.UUID | None
    config: InfrastructureConfig
    data_plane: DataPlane
    storage: ObjectStorageProvider

    @property
    def isolated(self) -> bool:
        return self.data_plane.isolated


@dataclass(slots=True)
class _Built:
    config: InfrastructureConfig
    data_plane: DataPlane
    storage: ObjectStorageProvider
    built_storage: BuiltStorage | None

    async def close(self) -> None:
        await self.data_plane.dispose()
        if self.built_storage is not None:
            self.built_storage.close()


def verify(row: InfrastructureProfileVersion) -> InfrastructureConfig:
    """Rebuild the stored configuration and check it wasn't altered."""
    config = from_stored(row.config)
    if config.checksum() != row.checksum:
        logger.error(
            "infrastructure profile checksum mismatch",
            extra={"version_id": str(row.id), "workspace_id": str(row.workspace_id)},
        )
        raise InfrastructureUnavailableError(
            "This workspace's infrastructure configuration failed an integrity check. "
            "Contact support.",
            code="infrastructure_integrity_error",
        )
    return config


class InfrastructureResolver:
    def __init__(
        self,
        control_sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        cipher: EnvelopeCipher | None,
        platform_storage: ObjectStorageProvider,
        *,
        cache_seconds: float = 10.0,
    ) -> None:
        self._control = control_sessions
        self._settings = settings
        self._cipher = cipher
        self._platform_storage = platform_storage
        self._ttl = cache_seconds
        self.shared = DataPlane("shared", control_sessions)
        self._mapping: dict[uuid.UUID, tuple[float, uuid.UUID | None]] = {}
        self._built: OrderedDict[uuid.UUID, _Built] = OrderedDict()
        self._lock = asyncio.Lock()

    def invalidate(self, workspace_id: uuid.UUID) -> None:
        self._mapping.pop(workspace_id, None)

    def platform(self, workspace_id: uuid.UUID) -> ResolvedInfrastructure:
        return ResolvedInfrastructure(
            workspace_id, None, PLATFORM_DEFAULT, self.shared, self._platform_storage
        )

    async def _version_for(self, workspace_id: uuid.UUID) -> uuid.UUID | None:
        now = time.monotonic()
        hit = self._mapping.get(workspace_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        async with self._control() as session:
            row = await session.get(WorkspaceInfrastructure, workspace_id)
        version_id = row.version_id if row is not None else None
        if len(self._mapping) > 50_000:
            self._mapping.clear()
        self._mapping[workspace_id] = (now, version_id)
        return version_id

    async def resolve(self, workspace_id: uuid.UUID) -> ResolvedInfrastructure:
        version_id = await self._version_for(workspace_id)
        if version_id is None:
            return self.platform(workspace_id)
        built = await self._built_for(version_id)
        return ResolvedInfrastructure(
            workspace_id, version_id, built.config, built.data_plane, built.storage
        )

    async def _built_for(self, version_id: uuid.UUID) -> _Built:
        cached = self._built.get(version_id)
        if cached is not None:
            self._built.move_to_end(version_id)
            return cached
        async with self._lock:
            cached = self._built.get(version_id)
            if cached is not None:
                return cached
            built = await self._build(version_id)
            self._built[version_id] = built
            while len(self._built) > self._settings.dataplane_max_open:
                _, evicted = self._built.popitem(last=False)
                await evicted.close()
            return built

    async def _secret(
        self, session: AsyncSession, workspace_id: uuid.UUID, cid: uuid.UUID | None
    ) -> str:
        if cid is None:
            raise InfrastructureUnavailableError("A credential for this infrastructure is missing.")
        vault = CredentialVault(session, self._cipher)
        row = await vault.get(workspace_id, cid)
        if row is None:
            raise InfrastructureUnavailableError("A credential for this infrastructure is missing.")
        try:
            return vault.reveal(row)
        except VaultError as exc:
            logger.error("infrastructure credential unreadable", extra={"credential_id": str(cid)})
            raise InfrastructureUnavailableError(
                "A credential for this infrastructure can't be read."
            ) from exc

    async def _build(self, version_id: uuid.UUID) -> _Built:
        async with self._control() as session:
            row = await session.get(InfrastructureProfileVersion, version_id)
            if row is None:
                raise InfrastructureUnavailableError("Infrastructure profile not found.")
            config = verify(row)
            await self._recheck(config)
            data_plane = self.shared
            if config.database.mode != "shared":
                password = await self._secret(
                    session, row.workspace_id, config.database.password_credential_id
                )
                engine = create_dataplane_engine(self._settings, config.database, password)
                data_plane = DataPlane(
                    str(version_id),
                    dataplane_sessions(engine),
                    engine,
                    processing_mode=config.processing.mode,
                )
            storage: ObjectStorageProvider = self._platform_storage
            built_storage: BuiltStorage | None = None
            if config.storage.provider != "documind":
                secret = await self._secret(
                    session, row.workspace_id, config.storage.secret_credential_id
                )
                built_storage = build_storage(self._settings, config.storage, secret)
                storage = built_storage.storage
            await session.commit()  # credential last_used_at
        return _Built(config, data_plane, storage, built_storage)

    async def _recheck(self, config: InfrastructureConfig) -> None:
        """Customer hosts are checked again before connecting (DNS may have changed)."""
        allowed = frozenset(self._settings.infrastructure_allowed_private_hosts)
        try:
            if config.database.mode == "external" and config.database.host:
                await check_host(
                    config.database.host, config.database.port or 5432, allowed=allowed
                )
            if config.storage.endpoint_url:
                await check_url(config.storage.endpoint_url, allowed=allowed)
        except BlockedHostError as exc:
            logger.error(
                "customer host now resolves to a blocked address", extra={"reason": str(exc)}
            )
            raise InfrastructureUnavailableError(
                "This organization's infrastructure points to an address that isn't allowed.",
                code="infrastructure_blocked",
            ) from exc

    async def isolated_planes(self) -> list[tuple[DataPlane, list[uuid.UUID]]]:
        """Every active dedicated or external data plane, with its workspaces (sweepers)."""
        async with self._control() as session:
            rows = (
                await session.execute(
                    select(WorkspaceInfrastructure.workspace_id, WorkspaceInfrastructure.version_id)
                    .join(
                        InfrastructureProfileVersion,
                        InfrastructureProfileVersion.id == WorkspaceInfrastructure.version_id,
                    )
                    .where(
                        InfrastructureProfileVersion.config["database"]["mode"].astext != "shared"
                    )
                )
            ).all()
        planes: list[tuple[DataPlane, list[uuid.UUID]]] = []
        for workspace_id, version_id in rows:
            try:
                built = await self._built_for(version_id)
            except ServiceUnavailableError:
                logger.warning("data plane unavailable", extra={"version_id": str(version_id)})
                continue
            planes.append((built.data_plane, [workspace_id]))
        return planes

    async def aclose(self) -> None:
        for built in self._built.values():
            await built.close()
        self._built.clear()


class InfrastructureTargets:
    """``ProcessingTargets`` for workers: the job's data plane and storage."""

    def __init__(self, resolver: InfrastructureResolver) -> None:
        self._resolver = resolver

    async def for_workspace(
        self, workspace_id: uuid.UUID | None
    ) -> tuple[Any, ObjectStorageProvider]:
        if workspace_id is None:
            return self._resolver.shared.sessions, self._resolver.platform(uuid.UUID(int=0)).storage
        infra = await self._resolver.resolve(workspace_id)
        return infra.data_plane.sessions, infra.storage
