"""Alembic environment for dedicated and external data planes (ADR 0021).

Run programmatically by ``documind.infrastructure.database.dataplane_migrations``
(provisioning and validation), or by hand:

    alembic -n dataplane -x database_url=postgresql+asyncpg://... upgrade head

Everything lives in the ``documind`` schema, so a customer's database can hold
other tables without collisions.
"""

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from documind.infrastructure.database.dataplane import (
    DATAPLANE_SCHEMA,
    DATAPLANE_VERSION_TABLE,
    dataplane_metadata,
)

config = context.config
target_metadata = dataplane_metadata()


def _database_url() -> str:
    url = config.attributes.get("database_url") or context.get_x_argument(as_dictionary=True).get(
        "database_url"
    )
    if not url:
        raise RuntimeError("Pass the data-plane URL: -x database_url=postgresql+asyncpg://...")
    return str(url)


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    if type_ == "schema":
        return name in (None, DATAPLANE_SCHEMA)
    if type_ == "table":
        return name != DATAPLANE_VERSION_TABLE
    return True


def _do_run_migrations(connection: Connection) -> None:
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DATAPLANE_SCHEMA}"'))
    connection.execute(text(f'SET search_path TO "{DATAPLANE_SCHEMA}", public'))
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=DATAPLANE_VERSION_TABLE,
        version_table_schema=DATAPLANE_SCHEMA,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run_migrations)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline mode isn't supported for data planes")
elif config.attributes.get("connection") is not None:
    # Called with an open (sync-facing) connection, e.g. from Connection.run_sync.
    _do_run_migrations(config.attributes["connection"])
else:
    asyncio.run(run_migrations_online())
