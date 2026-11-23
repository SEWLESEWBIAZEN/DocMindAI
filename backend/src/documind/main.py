"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from documind import __version__
from documind.api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from documind.api.router import api_router
from documind.container import Container, build_container
from documind.core.config import Settings, get_settings
from documind.core.logging import configure_logging

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[Settings], Container]


def create_app(
    settings: Settings | None = None,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Dependencies are connected lazily: the API starts even if a dependency
        # is temporarily down, and the readiness endpoint reports it.
        container = container_factory(settings)
        app.state.container = container
        logger.info(
            "Application started",
            extra={"environment": settings.environment.value, "version": __version__},
        )
        try:
            yield
        finally:
            await container.aclose()
            logger.info("Application stopped")

    docs = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    # Added last so it is the outermost middleware and sees every request.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router)
    return app
