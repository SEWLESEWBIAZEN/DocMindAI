"""Sample documents for trying DocuMind (ADR 0033)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from pydantic import BaseModel

from documind.api.dependencies import (
    CollectionServiceDep,
    DocumentServiceDep,
    QuotasDep,
    WorkspaceCtx,
)
from documind.application.onboarding.samples import SampleService, SampleStatus

router = APIRouter(prefix="/workspaces/{workspace_id}/samples", tags=["onboarding"])


class SampleStatusResponse(BaseModel):
    loaded: bool
    document_ids: list[uuid.UUID]
    collection_id: uuid.UUID | None
    questions: list[str]


class SampleRemovalResponse(BaseModel):
    removed: int


def _status(s: SampleStatus) -> SampleStatusResponse:
    return SampleStatusResponse(
        loaded=s.loaded,
        document_ids=s.document_ids,
        collection_id=s.collection_id,
        questions=list(s.questions),
    )


@router.get("", response_model=SampleStatusResponse, summary="Whether the samples are loaded")
async def sample_status(
    ctx: WorkspaceCtx, documents: DocumentServiceDep, collections: CollectionServiceDep
) -> SampleStatusResponse:
    return _status(await SampleService(documents, collections).status(ctx))


@router.post(
    "",
    response_model=SampleStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add DocuMind's sample documents (idempotent; counts towards quotas)",
)
async def load_samples(
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
    collections: CollectionServiceDep,
    quotas: QuotasDep,
) -> SampleStatusResponse:
    return _status(await SampleService(documents, collections).load(ctx, admit=quotas.upload))


@router.delete(
    "",
    response_model=SampleRemovalResponse,
    summary="Remove unchanged sample documents (no 2FA needed; they hold no user data)",
)
async def remove_samples(
    ctx: WorkspaceCtx, documents: DocumentServiceDep, collections: CollectionServiceDep
) -> SampleRemovalResponse:
    return SampleRemovalResponse(removed=await SampleService(documents, collections).remove(ctx))
