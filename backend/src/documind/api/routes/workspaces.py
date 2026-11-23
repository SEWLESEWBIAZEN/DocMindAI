"""Workspace-scoped endpoints. Every route resolves membership first (404 for non-members)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from documind.api.dependencies import (
    CurrentUser,
    InvitationServiceDep,
    WorkspaceCtx,
    WorkspaceServiceDep,
)
from documind.api.schemas.identity import (
    AcceptedInvitationResponse,
    AuditLogResponse,
    ChangeRoleRequest,
    CreateOrganizationRequest,
    InvitationResponse,
    InviteRequest,
    MemberResponse,
    RenameWorkspaceRequest,
    TokenRequest,
    WorkspaceResponse,
)
from documind.application.identity.invitation_service import InvitationInfo
from documind.application.identity.workspace_service import WorkspaceSummary

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["workspaces"])
organizations_router = APIRouter(prefix="/organizations", tags=["organizations"])
invitations_router = APIRouter(prefix="/invitations", tags=["invitations"])


def workspace_response(w: WorkspaceSummary) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=w.id,
        kind=w.kind,
        name=w.name,
        role=w.role,
        organization_id=w.organization_id,
        organization_slug=w.organization_slug,
        is_default=w.is_default,
    )


def invitation_response(i: InvitationInfo) -> InvitationResponse:
    return InvitationResponse(
        id=i.id,
        workspace_id=i.workspace_id,
        workspace_name=i.workspace_name,
        email=i.email,
        role=i.role,
        status=i.status,
        created_at=i.created_at,
        expires_at=i.expires_at,
    )


# --- Organizations ------------------------------------------------------------


@organizations_router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=WorkspaceResponse
)
async def create_organization(
    body: CreateOrganizationRequest, user: CurrentUser, workspaces: WorkspaceServiceDep
) -> WorkspaceResponse:
    return workspace_response(await workspaces.create_organization(user, body.name))


# --- Workspace ----------------------------------------------------------------


@router.get("", response_model=WorkspaceResponse)
async def get_workspace(
    ctx: WorkspaceCtx, user: CurrentUser, workspaces: WorkspaceServiceDep
) -> WorkspaceResponse:
    return workspace_response(await workspaces.get(ctx, user))


@router.patch("", response_model=WorkspaceResponse)
async def rename_workspace(
    body: RenameWorkspaceRequest,
    ctx: WorkspaceCtx,
    user: CurrentUser,
    workspaces: WorkspaceServiceDep,
) -> WorkspaceResponse:
    return workspace_response(await workspaces.rename(ctx, user, body.name))


@router.post("/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_workspace(ctx: WorkspaceCtx, workspaces: WorkspaceServiceDep) -> Response:
    await workspaces.leave(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Members ------------------------------------------------------------------


@router.get("/members", response_model=list[MemberResponse])
async def list_members(ctx: WorkspaceCtx, workspaces: WorkspaceServiceDep) -> list[MemberResponse]:
    return [
        MemberResponse(
            id=m.id,
            user_id=m.user_id,
            email=m.email,
            display_name=m.display_name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m in await workspaces.list_members(ctx)
    ]


@router.patch("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def change_member_role(
    member_id: uuid.UUID,
    body: ChangeRoleRequest,
    ctx: WorkspaceCtx,
    workspaces: WorkspaceServiceDep,
) -> Response:
    await workspaces.change_role(ctx, member_id, body.role)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: uuid.UUID, ctx: WorkspaceCtx, workspaces: WorkspaceServiceDep
) -> Response:
    await workspaces.remove_member(ctx, member_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Invitations (workspace side) --------------------------------------------


@router.get("/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    ctx: WorkspaceCtx, invitations: InvitationServiceDep
) -> list[InvitationResponse]:
    return [invitation_response(i) for i in await invitations.list_pending(ctx)]


@router.post("/invitations", status_code=status.HTTP_201_CREATED, response_model=InvitationResponse)
async def create_invitation(
    body: InviteRequest, ctx: WorkspaceCtx, user: CurrentUser, invitations: InvitationServiceDep
) -> InvitationResponse:
    return invitation_response(await invitations.invite(ctx, user, str(body.email), body.role))


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID, ctx: WorkspaceCtx, invitations: InvitationServiceDep
) -> Response:
    await invitations.revoke(ctx, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Audit log ----------------------------------------------------------------


@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def audit_logs(
    ctx: WorkspaceCtx,
    workspaces: WorkspaceServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[
        int | None, Query(ge=1, description="Cursor: return entries older than this id")
    ] = None,
) -> list[AuditLogResponse]:
    return [
        AuditLogResponse(
            id=e.id,
            created_at=e.created_at,
            action=e.action,
            actor_user_id=e.actor_user_id,
            target_type=e.target_type,
            target_id=e.target_id,
            details=e.details,
            ip_address=e.ip_address,
        )
        for e in await workspaces.audit_log(ctx, limit=limit, before_id=before_id)
    ]


# --- Invitations (invitee side, by emailed token) ----------------------------


@invitations_router.post("/accept", response_model=AcceptedInvitationResponse)
async def accept_invitation_by_token(
    body: TokenRequest, user: CurrentUser, invitations: InvitationServiceDep
) -> AcceptedInvitationResponse:
    return AcceptedInvitationResponse(
        workspace_id=await invitations.accept_by_token(user, body.token)
    )
