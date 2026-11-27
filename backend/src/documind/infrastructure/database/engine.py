"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from documind.core.config import Settings


def server_settings(settings: Settings, application_name: str) -> dict[str, str]:
    """Per-connection PostgreSQL settings, including the statement timeout if one is set."""
    values = {"application_name": application_name}
    timeout_ms = int(settings.database_statement_timeout_seconds * 1000)
    if timeout_ms > 0:
        values["statement_timeout"] = str(timeout_ms)
    return values


def create_engine(settings: Settings, *, application_name: str = "documind-api") -> AsyncEngine:
    """Create the process-wide engine. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        echo=settings.database_echo,
        connect_args={"server_settings": server_settings(settings, application_name)},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
