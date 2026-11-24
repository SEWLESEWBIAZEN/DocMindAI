"""Organization infrastructure settings (MI-1 to MI-5, ADR 0021). Owners only."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import ContainerDep, MetaDep, SessionDep, WorkspaceCtx
from documind.application.infrastructure.service import InfrastructureService, VersionView

router = APIRouter(prefix="/workspaces/{workspace_id}/infrastructure", tags=["infrastructure"])


class DatabaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["shared", "dedicated", "external"] = "shared"
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=63)
    username: str | None = Field(default=None, max_length=63)
    sslmode: Literal["disable", "require", "verify-ca", "verify-full"] | None = None
    manage_schema: bool = False
    password: str | None = Field(
        default=None, max_length=512, description="Write-only; stored encrypted (external only)"
    )


class StorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["documind", "aws_s3", "s3_compatible", "minio", "azure_blob"] = "documind"
    bucket: str | None = Field(default=None, max_length=63)
    region: str | None = Field(default=None, max_length=32)
    endpoint_url: str | None = Field(default=None, max_length=300)
    prefix: str = Field(default="", max_length=200)
    force_path_style: bool | None = None
    access_key_id: str | None = Field(default=None, max_length=128)
    secret_access_key: str | None = Field(
        default=None, max_length=512, description="Write-only; stored encrypted"
    )


class ProcessingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["documind", "customer", "hybrid"] = "documind"


class CreateVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: DatabaseInput = Field(default_factory=DatabaseInput)
    storage: StorageInput = Field(default_factory=StorageInput)
    processing: ProcessingInput = Field(default_factory=ProcessingInput)
    note: str | None = Field(default=None, max_length=255)


class VersionResponse(BaseModel):
    id: uuid.UUID
    version: int
    status: str = Field(description="draft, validated, failed, active or retired")
    config: dict[str, Any] = Field(description="Non-secret configuration; secrets are references")
    checksum: str
    validation: dict[str, Any]
    note: str | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None
    validated_at: datetime | None
    activated_at: datetime | None


class InfrastructureResponse(BaseModel):
    workspace_kind: str
    active_version_id: uuid.UUID | None = Field(description="None: DocuMind platform defaults")
    active_config: dict[str, Any]
    versions: list[VersionResponse]
    entitlements: dict[str, bool]
    storage_providers: list[dict[str, str]]
    dedicated_available: bool
    events: list[dict[str, Any]]
    workers: list[dict[str, Any]]
    migrations: list[dict[str, Any]]
    has_data: bool


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class WorkerTokenResponse(BaseModel):
    id: uuid.UUID
    name: str
    token: str = Field(description="Shown once. Store it in the worker's configuration.")


def get_infrastructure_service(
    session: SessionDep, container: ContainerDep, meta: MetaDep
) -> InfrastructureService:
    return InfrastructureService(
        session, container.settings, container.cipher, container.infrastructure(), meta
    )


ServiceDep = Annotated[InfrastructureService, Depends(get_infrastructure_service)]


def _version(v: VersionView) -> VersionResponse:
    return VersionResponse(**{f: getattr(v, f) for f in VersionResponse.model_fields})


@router.get("", response_model=InfrastructureResponse, summary="Infrastructure settings (owners)")
async def get_infrastructure(ctx: WorkspaceCtx, infra: ServiceDep) -> InfrastructureResponse:
    o = await infra.overview(ctx)
    return InfrastructureResponse(
        workspace_kind=o.workspace_kind,
        active_version_id=o.active_version_id,
        active_config=o.active_config,
        versions=[_version(v) for v in o.versions],
        entitlements=o.entitlements,
        storage_providers=o.storage_providers,
        dedicated_available=o.dedicated_available,
        events=o.events,
        workers=o.workers,
        migrations=o.migrations,
        has_data=o.has_data,
    )


@router.post(
    "/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionResponse,
    summary="Create a draft version (2FA step-up; secrets are stored encrypted)",
)
async def create_version(
    body: CreateVersionRequest, ctx: WorkspaceCtx, infra: ServiceDep
) -> VersionResponse:
    raw = body.model_dump(exclude={"note"})
    password = raw["database"].pop("password", None)
    secret = raw["storage"].pop("secret_access_key", None)
    if raw["storage"].get("force_path_style") is None:
        raw["storage"].pop("force_path_style", None)
    view = await infra.create_version(
        ctx, raw, database_password=password, storage_secret_key=secret, note=body.note
    )
    return _version(view)


@router.post(
    "/versions/{version_id}/validate",
    response_model=VersionResponse,
    summary="Check connectivity, permissions and schema for a draft",
)
async def validate_version(
    version_id: uuid.UUID, ctx: WorkspaceCtx, infra: ServiceDep
) -> VersionResponse:
    return _version(await infra.validate(ctx, version_id))


@router.post(
    "/versions/{version_id}/activate",
    response_model=VersionResponse,
    summary="Activate a validated version (2FA step-up; 409 migration_required if data must move)",
)
async def activate_version(
    version_id: uuid.UUID, ctx: WorkspaceCtx, infra: ServiceDep
) -> VersionResponse:
    return _version(await infra.activate(ctx, version_id))


@router.post(
    "/migrations/{migration_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a planned migration",
)
async def cancel_migration(
    migration_id: uuid.UUID, ctx: WorkspaceCtx, infra: ServiceDep
) -> Response:
    await infra.cancel_migration(ctx, migration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workers",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkerTokenResponse,
    summary="Register a customer-hosted worker (MI-7 foundation; 2FA step-up)",
)
async def register_worker(
    body: WorkerRequest, ctx: WorkspaceCtx, infra: ServiceDep
) -> WorkerTokenResponse:
    issued = await infra.register_worker(ctx, body.name)
    return WorkerTokenResponse(id=issued.id, name=issued.name, token=issued.token)


@router.delete(
    "/workers/{worker_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke a worker"
)
async def revoke_worker(worker_id: uuid.UUID, ctx: WorkspaceCtx, infra: ServiceDep) -> Response:
    await infra.revoke_worker(ctx, worker_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
