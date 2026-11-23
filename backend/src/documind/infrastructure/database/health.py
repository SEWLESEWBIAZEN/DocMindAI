"""PostgreSQL / pgvector health check."""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from documind.domain.health import DependencyCheckError, DetailValue


class PostgresHealthCheck:
    name = "database"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> Mapping[str, DetailValue]:
        async with self._engine.connect() as conn:
            server_version = await conn.scalar(text("SHOW server_version"))
            pgvector_version = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        if pgvector_version is None:
            raise DependencyCheckError("pgvector extension is not installed; run migrations")
        return {"server_version": str(server_version), "pgvector_version": str(pgvector_version)}
