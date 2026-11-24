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

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import ContainerDep, SessionDep
from documind.application.ai.metering import UsageEvent
from documind.application.identity.common import utcnow
from documind.application.infrastructure.service import authenticate_worker
from documind.domain.billing.plans import BilledTo, Capability
from documind.domain.errors import AuthenticationError, PermissionDeniedError
from documind.infrastructure.database.models import WorkerRegistration

router = APIRouter(prefix="/worker/v1", tags=["customer workers"])
_bearer = HTTPBearer(auto_error=False, description="Worker token from registration")


class ProcessingContract(BaseModel):
    embedding_model: str = Field(description="The worker must embed with this model")
    embedding_dimensions: int


class WorkerSessionResponse(BaseModel):
    worker_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    scopes: list[str]
    last_seen_at: datetime
    processing: ProcessingContract


class UsageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID
    embedding_model: str = Field(min_length=1, max_length=100)
    input_tokens: int = Field(ge=0, le=100_000_000)
    outcome: str = Field(max_length=32)


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
    session: SessionDep,
    container: ContainerDep,
    worker: Annotated[WorkerRegistration, Depends(current_worker)],
) -> WorkerSessionResponse:
    worker.last_seen_at = utcnow()
    await session.commit()
    ai = await container.ai_resolver().resolve(worker.workspace_id)
    return WorkerSessionResponse(
        worker_id=worker.id,
        workspace_id=worker.workspace_id,
        name=worker.name,
        scopes=list(worker.scopes),
        last_seen_at=worker.last_seen_at,
        processing=ProcessingContract(
            embedding_model=ai.embeddings.model, embedding_dimensions=ai.embeddings.dimensions
        ),
    )


@router.post(
    "/usage",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Report usage of a processed job (shown to the organization, billed to it)",
)
async def report_usage(
    body: UsageReport,
    container: ContainerDep,
    worker: Annotated[WorkerRegistration, Depends(current_worker)],
) -> Response:
    if "jobs:complete" not in worker.scopes:
        raise PermissionDeniedError("This worker token can't report jobs.")
    # The worker runs on the customer's resources: billed to the workspace, not the platform.
    await container.ai_resolver().sink.record(
        UsageEvent(
            workspace_id=worker.workspace_id,
            capability=Capability.EMBEDDING,
            provider="customer_worker",
            model=body.embedding_model,
            billed_to=BilledTo.WORKSPACE_KEY,
            input_tokens=body.input_tokens,
            units=1,
            source_type="processing_job",
            source_id=str(body.job_id),
            idempotency_key=f"customer-worker:{body.job_id}:{body.outcome}",
            details={"worker_id": str(worker.id), "outcome": body.outcome},
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
