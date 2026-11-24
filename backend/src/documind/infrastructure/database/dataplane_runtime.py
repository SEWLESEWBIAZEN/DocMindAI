"""Engines, migrations, tenant registration and the audit outbox for data planes."""

from __future__ import annotations

import logging
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from documind.core.config import Settings
from documind.domain.infrastructure.profiles import DatabaseConfig
from documind.infrastructure.database.dataplane import (
    DATAPLANE_SCHEMA,
    DATAPLANE_VERSION_TABLE,
    AuditOutbox,
    TenantWorkspace,
)
from documind.infrastructure.database.models import AuditLog

logger = logging.getLogger(__name__)

PLANE_INFO_KEY = "documind_plane"
ISOLATED = "isolated"
_ALEMBIC_INI = Path(__file__).resolve().parents[4] / "alembic.ini"


def is_isolated(session: AsyncSession) -> bool:
    return session.info.get(PLANE_INFO_KEY) == ISOLATED


@dataclass(slots=True)
class DataPlane:
    """Where a workspace's documents and conversations live."""

    key: str  # "shared" or the profile version id
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine | None = None  # None: the shared (control-plane) engine

    @property
    def isolated(self) -> bool:
        return self.engine is not None

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


def _ssl_context(mode: str | None) -> ssl.SSLContext | bool:
    if mode in (None, "disable"):
        return False
    context = ssl.create_default_context()
    if mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        context.check_hostname = False
    return context


def database_url(config: DatabaseConfig, password: str) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=config.username,
        password=password,
        host=config.host,
        port=config.port or 5432,
        database=config.database,
    )


def create_dataplane_engine(
    settings: Settings, config: DatabaseConfig, password: str, *, pool_size: int | None = None
) -> AsyncEngine:
    return create_async_engine(
        database_url(config, password),
        pool_size=pool_size or settings.dataplane_pool_size,
        max_overflow=0,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "ssl": _ssl_context(config.sslmode),
            "timeout": 10,
            "server_settings": {
                "application_name": "documind-dataplane",
                "search_path": f"{DATAPLANE_SCHEMA},public",
            },
        },
    )


def dataplane_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False, info={PLANE_INFO_KEY: ISOLATED}
    )


async def migrate(engine: AsyncEngine) -> str | None:
    """Bring the data-plane schema to head. Returns the resulting revision."""
    from alembic import command
    from alembic.config import Config

    async with engine.begin() as conn:

        def run(sync_conn: Any) -> None:
            cfg = Config(str(_ALEMBIC_INI), ini_section="dataplane")
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(run)
    return await schema_revision(engine)


def head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(
        Config(str(_ALEMBIC_INI), ini_section="dataplane")
    ).get_heads()
    return str(heads[0])


async def schema_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        exists = (
            await conn.execute(
                text("SELECT to_regclass(:name)"),
                {"name": f"{DATAPLANE_SCHEMA}.{DATAPLANE_VERSION_TABLE}"},
            )
        ).scalar()
        if exists is None:
            return None
        return (
            await conn.execute(
                # Constants only (no user input) in the identifier.
                text(f'SELECT version_num FROM "{DATAPLANE_SCHEMA}".{DATAPLANE_VERSION_TABLE}')  # noqa: S608
            )
        ).scalar()


async def register_tenant(
    sessions: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> None:
    async with sessions() as session:
        await session.execute(
            insert(TenantWorkspace)
            .values(workspace_id=workspace_id)
            .on_conflict_do_nothing(index_elements=["workspace_id"])
        )
        await session.commit()


async def tenant_registered(
    sessions: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> bool:
    async with sessions() as session:
        return await session.get(TenantWorkspace, workspace_id) is not None


# --------------------------------------------------------------------------- #
# Audit outbox
# --------------------------------------------------------------------------- #


class OutboxAuditRepository:
    """Drop-in for ``AuditLogRepository.add`` in isolated data planes."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, entry: AuditLog) -> None:
        self._s.add(
            AuditOutbox(
                workspace_id=entry.workspace_id,
                actor_user_id=entry.actor_user_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                details=entry.details or {},
                ip_address=entry.ip_address,
                user_agent=entry.user_agent,
                request_id=entry.request_id,
            )
        )


async def relay_outbox(
    data: async_sessionmaker[AsyncSession],
    control: async_sessionmaker[AsyncSession],
    *,
    batch: int = 500,
) -> int:
    """Copy outbox entries to ``audit_logs`` (idempotent by event id), then delete them."""
    moved = 0
    while True:
        async with data() as dsession:
            rows = list(
                (
                    await dsession.execute(
                        select(AuditOutbox)
                        .order_by(AuditOutbox.created_at)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not rows:
                await dsession.commit()
                return moved
            async with control() as csession:
                await csession.execute(
                    insert(AuditLog)
                    .values(
                        [
                            {
                                "event_id": r.event_id,
                                "created_at": r.created_at,
                                "workspace_id": r.workspace_id,
                                "actor_user_id": r.actor_user_id,
                                "action": r.action,
                                "target_type": r.target_type,
                                "target_id": r.target_id,
                                "details": r.details,
                                "ip_address": r.ip_address,
                                "user_agent": r.user_agent,
                                "request_id": r.request_id,
                            }
                            for r in rows
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
                await csession.commit()
            await dsession.execute(
                delete(AuditOutbox).where(AuditOutbox.event_id.in_([r.event_id for r in rows]))
            )
            await dsession.commit()
            moved += len(rows)
            if len(rows) < batch:
                return moved
