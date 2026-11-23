"""Health endpoints.

* ``GET /api/v1/health/live``  - liveness: the process is up. Never touches dependencies.
* ``GET /api/v1/health``       - readiness: application and dependency health.
  Returns 200 when every dependency is healthy, otherwise 503.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from documind import __version__
from documind.api.dependencies import HealthServiceDep, SettingsDep
from documind.domain.health import ComponentStatus, DetailValue, OverallStatus

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class AppInfo(BaseModel):
    name: str
    version: str
    environment: str


class ComponentHealthResponse(BaseModel):
    status: ComponentStatus
    latency_ms: float
    details: dict[str, DetailValue] | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: OverallStatus
    timestamp: datetime
    app: AppInfo
    components: dict[str, ComponentHealthResponse]


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "",
    response_model=HealthResponse,
    summary="Application and dependency health",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def readiness(
    response: Response,
    health_service: HealthServiceDep,
    settings: SettingsDep,
) -> HealthResponse:
    report = await health_service.check_readiness()
    # Version strings and error reasons help operators but also help attackers;
    # outside production they are shown, in production only status is.
    verbose = not settings.is_production
    if not report.is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status=report.status,
        timestamp=datetime.now(UTC),
        app=AppInfo(name=settings.app_name, version=__version__, environment=settings.environment),
        components={
            c.name: ComponentHealthResponse(
                status=c.status,
                latency_ms=c.latency_ms,
                details=dict(c.details) if verbose else None,
                error=c.error if verbose else None,
            )
            for c in report.components
        },
    )
