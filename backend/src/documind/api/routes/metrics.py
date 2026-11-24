"""Prometheus scrape endpoint, protected by METRICS_TOKEN (404 when unset)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from documind.api.dependencies import ContainerDep
from documind.ops import metrics

router = APIRouter(tags=["operations"])
_bearer = HTTPBearer(auto_error=False)


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def scrape(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> PlainTextResponse:
    expected = container.settings.metrics_token
    if expected is None:
        raise HTTPException(status_code=404)
    given = credentials.credentials if credentials else ""
    if not hmac.compare_digest(given.encode(), expected.get_secret_value().encode()):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(
        await metrics.render(container), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
