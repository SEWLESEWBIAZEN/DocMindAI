"""FastAPI dependency providers. Routes depend on these, never on globals."""

from typing import Annotated, cast

from fastapi import Depends, Request

from documind.application.health_service import HealthService
from documind.container import Container
from documind.core.config import Settings


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def get_settings_dep(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings


def get_health_service(container: Annotated[Container, Depends(get_container)]) -> HealthService:
    return container.health_service


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
