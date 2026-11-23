"""The signed-in user's account, workspaces and invitations."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from documind.api.dependencies import (
    AccountServiceDep,
    CurrentUser,
    InvitationServiceDep,
    SettingsDep,
    WorkspaceServiceDep,
)
from documind.api.routes._identity_common import token_response, user_response
from documind.api.routes.workspaces import invitation_response, workspace_response
from documind.api.schemas.identity import (
    AcceptedInvitationResponse,
    ActiveWorkspaceRequest,
    ChangePasswordRequest,
    InvitationResponse,
    ProfileUpdateRequest,
    TokenResponse,
    UserResponse,
    WorkspaceResponse,
)

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserResponse)
async def get_me(user: CurrentUser) -> UserResponse:
    return user_response(user)


@router.patch("", response_model=UserResponse)
async def update_me(
    body: ProfileUpdateRequest, user: CurrentUser, account: AccountServiceDep
) -> UserResponse:
    return user_response(await account.update_profile(user, body.display_name))


@router.post(
    "/password",
    response_model=TokenResponse,
    summary="Change password; signs out other sessions and returns a fresh one",
)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    user: CurrentUser,
    account: AccountServiceDep,
    settings: SettingsDep,
) -> TokenResponse:
    result = await account.change_password(user, body.current_password, body.new_password)
    return token_response(response, settings, result)


@router.get("/workspaces", response_model=list[WorkspaceResponse])
async def my_workspaces(
    user: CurrentUser, workspaces: WorkspaceServiceDep
) -> list[WorkspaceResponse]:
    return [workspace_response(w) for w in await workspaces.list_for_user(user)]


@router.put(
    "/active-workspace",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Switch the default (active) workspace",
)
async def set_active_workspace(
    body: ActiveWorkspaceRequest, user: CurrentUser, workspaces: WorkspaceServiceDep
) -> Response:
    await workspaces.set_default(user, body.workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/invitations", response_model=list[InvitationResponse])
async def my_invitations(
    user: CurrentUser, invitations: InvitationServiceDep
) -> list[InvitationResponse]:
    return [invitation_response(i) for i in await invitations.list_for_user(user)]


@router.post("/invitations/{invitation_id}/accept", response_model=AcceptedInvitationResponse)
async def accept_invitation(
    invitation_id: uuid.UUID, user: CurrentUser, invitations: InvitationServiceDep
) -> AcceptedInvitationResponse:
    return AcceptedInvitationResponse(
        workspace_id=await invitations.accept_by_id(user, invitation_id)
    )


@router.post("/invitations/{invitation_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invitation(
    invitation_id: uuid.UUID, user: CurrentUser, invitations: InvitationServiceDep
) -> Response:
    await invitations.decline_by_id(user, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
