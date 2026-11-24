"""Workspace AI provider keys (BYOK, ADR 0019) and re-indexing."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import (
    ContainerDep,
    DataSessionDep,
    MetaDep,
    SessionDep,
    WorkspaceCtx,
    limit_outbound_checks,
)
from documind.application.ai.byok import ProviderKeyService, ProviderView
from documind.application.processing.reindex import ReindexEstimate, dispatch_all

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["ai providers"])


class ProviderCatalogEntry(BaseModel):
    key: str
    name: str
    status: str = Field(description='"available" or "planned" (not selectable yet)')
    capabilities: list[str]
    settings: list[str]


class ProviderResponse(BaseModel):
    provider: str
    name: str
    status: str = Field(description='"active" or "invalid" (rejected by the provider)')
    hint: str | None = Field(description="Last 4 characters of the key, e.g. …a1b2")
    capabilities: list[str]
    settings: dict[str, str]
    models: dict[str, str]
    last_error_code: str | None
    last_validated_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None


class AIProvidersResponse(BaseModel):
    byok_allowed: bool
    platform_ai_allowed: bool
    key_only: bool
    providers: list[ProviderResponse]
    catalog: list[ProviderCatalogEntry]
    effective: dict[str, str] = Field(
        description=(
            'Per capability: "workspace_key", "platform", or why it is unavailable '
            '("provider_key_required", "provider_key_invalid", "capability_unavailable").'
        )
    )
    embedding_model: str


class PutKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=512, description="Write-only; never returned")
    settings: dict[str, str] = Field(default_factory=dict, description="Optional model overrides")
    reindex: bool = Field(
        default=False,
        description="Confirm re-indexing when the embedding model changes (otherwise 409)",
    )


class KeyOnlyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_only: bool


class KeyOnlyResponse(BaseModel):
    key_only: bool


class ReindexResponse(BaseModel):
    target_model: str
    current_models: list[str]
    documents: int
    chunks: int
    tokens: int
    required: bool
    queued: int = 0


def get_provider_keys(
    session: SessionDep, data: DataSessionDep, container: ContainerDep, meta: MetaDep
) -> ProviderKeyService:
    return ProviderKeyService(
        session,
        container.settings,
        container.cipher,
        container.require_provider_factory(),
        container.ai_resolver(),
        container.workspace_keys,
        meta,
        data_session=data,
    )


ProviderKeysDep = Annotated[ProviderKeyService, Depends(get_provider_keys)]


def _provider(v: ProviderView) -> ProviderResponse:
    return ProviderResponse(
        provider=v.provider,
        name=v.name,
        status=v.status,
        hint=v.hint,
        capabilities=list(v.capabilities),
        settings=v.settings,
        models=v.models,
        last_error_code=v.last_error_code,
        last_validated_at=v.last_validated_at,
        last_used_at=v.last_used_at,
        created_at=v.created_at,
        created_by_user_id=v.created_by_user_id,
    )


def _estimate(e: ReindexEstimate, queued: int = 0) -> ReindexResponse:
    return ReindexResponse(
        target_model=e.target_model,
        current_models=list(e.current_models),
        documents=e.documents,
        chunks=e.chunks,
        tokens=e.tokens,
        required=e.required,
        queued=queued,
    )


@router.get(
    "/ai-providers",
    response_model=AIProvidersResponse,
    summary="AI provider keys and which key each capability uses (ai_providers.manage)",
)
async def get_ai_providers(ctx: WorkspaceCtx, keys: ProviderKeysDep) -> AIProvidersResponse:
    o = await keys.overview(ctx)
    return AIProvidersResponse(
        byok_allowed=o.byok_allowed,
        platform_ai_allowed=o.platform_ai_allowed,
        key_only=o.key_only,
        providers=[_provider(p) for p in o.providers],
        catalog=[
            ProviderCatalogEntry(
                key=c.key,
                name=c.name,
                status=c.status,
                capabilities=sorted(x.value for x in c.capabilities),
                settings=list(c.settings),
            )
            for c in o.catalog
        ],
        effective=o.effective,
        embedding_model=o.embedding_model,
    )


@router.put(
    "/ai-providers/{provider}",
    response_model=ProviderResponse,
    dependencies=[Depends(limit_outbound_checks)],
    summary="Add or replace the workspace's key for a provider (2FA step-up required)",
    description=(
        "The key is validated with the provider before it is saved, then stored encrypted. "
        "It is never returned. Returns 409 `reindex_required` when the embedding model would "
        "change and `reindex` is false."
    ),
)
async def put_ai_provider(
    provider: str,
    body: PutKeyRequest,
    ctx: WorkspaceCtx,
    keys: ProviderKeysDep,
    container: ContainerDep,
) -> ProviderResponse:
    view = await keys.put_key(
        ctx, provider, body.api_key, dict(body.settings), reindex=body.reindex
    )
    await dispatch_all(container.job_dispatcher, keys.queued_jobs, workspace_id=ctx.workspace_id)
    return _provider(view)


@router.delete(
    "/ai-providers/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the workspace's key (2FA step-up required)",
)
async def delete_ai_provider(
    provider: str,
    ctx: WorkspaceCtx,
    keys: ProviderKeysDep,
    container: ContainerDep,
    reindex: Annotated[bool, Query()] = False,
) -> Response:
    await keys.remove_key(ctx, provider, reindex=reindex)
    await dispatch_all(container.job_dispatcher, keys.queued_jobs, workspace_id=ctx.workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/ai-providers/{provider}/validate",
    response_model=ProviderResponse,
    dependencies=[Depends(limit_outbound_checks)],
    summary="Check a saved key again (e.g. after fixing billing with the provider)",
)
async def validate_ai_provider(
    provider: str, ctx: WorkspaceCtx, keys: ProviderKeysDep
) -> ProviderResponse:
    return _provider(await keys.revalidate(ctx, provider))


@router.put(
    "/ai-settings",
    response_model=KeyOnlyResponse,
    summary='"Use my key only": never use DocuMind\'s key (turning it off needs 2FA)',
)
async def put_ai_settings(
    body: KeyOnlyRequest, ctx: WorkspaceCtx, keys: ProviderKeysDep
) -> KeyOnlyResponse:
    return KeyOnlyResponse(key_only=await keys.set_key_only(ctx, body.key_only))


@router.get("/reindex", response_model=ReindexResponse, summary="Documents needing re-indexing")
async def reindex_estimate(ctx: WorkspaceCtx, keys: ProviderKeysDep) -> ReindexResponse:
    return _estimate(await keys.reindex_estimate(ctx))


@router.post(
    "/reindex",
    response_model=ReindexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-index documents embedded with another model",
)
async def start_reindex(
    ctx: WorkspaceCtx, keys: ProviderKeysDep, container: ContainerDep
) -> ReindexResponse:
    estimate, queued = await keys.start_reindex(ctx)
    await dispatch_all(container.job_dispatcher, keys.queued_jobs, workspace_id=ctx.workspace_id)
    return _estimate(estimate, queued)
