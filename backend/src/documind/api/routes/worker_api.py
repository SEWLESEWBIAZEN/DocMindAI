"""Customer-hosted worker API (MI-7, first endpoint).

Workers authenticate with the token issued at registration (``Authorization:
Bearer dmw_...``). Revoked or unknown tokens get 401. This endpoint is the
heartbeat every worker calls first; the job protocol (claim, download, report
results) builds on the same authentication.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from documind.api.dependencies import ContainerDep, SessionDep
from documind.application.identity.common import utcnow
from documind.application.infrastructure.service import authenticate_worker
from documind.domain.errors import AuthenticationError
from documind.infrastructure.database.models import WorkerRegistration

router = APIRouter(prefix="/worker/v1", tags=["customer workers"])
_bearer = HTTPBearer(auto_error=False, description="Worker token from registration")


class WorkerSessionResponse(BaseModel):
    worker_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    scopes: list[str]
    last_seen_at: datetime


async def current_worker(
    request: Request,
    session: SessionDep,
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> WorkerRegistration:
    client = request.client.host if request.client else "unknown"
    # Throttle guessing per client address.
    await container.rate_limiter.hit("worker:ip", client, 120, 60)
    token = (
        credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    )
    worker = await authenticate_worker(session, token) if token.startswith("dmw_") else None
    if worker is None:
        raise AuthenticationError("Invalid or revoked worker token.", code="invalid_worker_token")
    return worker


@router.post(
    "/heartbeat", response_model=WorkerSessionResponse, summary="Authenticate and check in"
)
async def heartbeat(
    session: SessionDep, worker: Annotated[WorkerRegistration, Depends(current_worker)]
) -> WorkerSessionResponse:
    worker.last_seen_at = utcnow()
    await session.commit()
    return WorkerSessionResponse(
        worker_id=worker.id,
        workspace_id=worker.workspace_id,
        name=worker.name,
        scopes=list(worker.scopes),
        last_seen_at=worker.last_seen_at,
    )
