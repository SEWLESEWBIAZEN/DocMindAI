"""The signed-in user's account, workspaces and invitations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Response, status

from documind.api.dependencies import (
    AccountServiceDep,
    CurrentUser,
    InvitationServiceDep,
    MfaServiceDep,
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
    MessageResponse,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaEmailConfirmRequest,
    MfaRegenerateRequest,
    MfaSetupRequest,
    MfaSetupResponse,
    MfaStatusResponse,
    PasskeyOptionsRequest,
    PasskeyRegisteredResponse,
    PasskeyRegisterRequest,
    PasskeyResponse,
    ProfileUpdateRequest,
    RecoveryCodesResponse,
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


# --- Two-factor authentication -----------------------------------------------


@router.get("/mfa", response_model=MfaStatusResponse, tags=["mfa"])
async def mfa_status(user: CurrentUser, mfa: MfaServiceDep) -> MfaStatusResponse:
    status_ = await mfa.status(user)
    return MfaStatusResponse(
        enabled=status_.enabled,
        method=status_.method,
        enabled_at=status_.enabled_at,
        recovery_codes_remaining=status_.recovery_codes_remaining,
    )


@router.post(
    "/mfa/totp/setup",
    response_model=MfaSetupResponse,
    tags=["mfa"],
    summary="Start authenticator-app setup (requires your password)",
)
async def mfa_setup(
    body: MfaSetupRequest, response: Response, user: CurrentUser, mfa: MfaServiceDep
) -> MfaSetupResponse:
    setup = await mfa.begin_setup(user, body.password)
    response.headers["Cache-Control"] = "no-store"
    return MfaSetupResponse(
        secret=setup.secret, otpauth_uri=setup.otpauth_uri, expires_at=setup.expires_at
    )


@router.post(
    "/mfa/totp/confirm",
    response_model=RecoveryCodesResponse,
    tags=["mfa"],
    summary="Confirm with a code from the app; turns 2FA on and returns recovery codes",
)
async def mfa_confirm(
    body: MfaCodeRequest, response: Response, user: CurrentUser, mfa: MfaServiceDep
) -> RecoveryCodesResponse:
    codes = await mfa.confirm_setup(user, body.code)
    response.headers["Cache-Control"] = "no-store"
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT, tags=["mfa"])
async def mfa_disable(body: MfaDisableRequest, user: CurrentUser, mfa: MfaServiceDep) -> Response:
    await mfa.disable(
        user, body.password, body.code, body.recovery_code, body.email_code, body.passkey
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/mfa/email/setup",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
    tags=["mfa"],
    summary="Start email-only 2FA, without an authenticator app (requires your password)",
)
async def mfa_email_setup(
    body: MfaSetupRequest, user: CurrentUser, mfa: MfaServiceDep
) -> MessageResponse:
    await mfa.begin_email_setup(user, body.password)
    return MessageResponse(detail="We sent a one-time code to your email address.")


@router.post(
    "/mfa/email/confirm",
    response_model=RecoveryCodesResponse,
    tags=["mfa"],
    summary="Confirm with the emailed code; turns email 2FA on and returns recovery codes",
)
async def mfa_email_confirm(
    body: MfaEmailConfirmRequest, response: Response, user: CurrentUser, mfa: MfaServiceDep
) -> RecoveryCodesResponse:
    codes = await mfa.confirm_email_setup(user, body.email_code)
    response.headers["Cache-Control"] = "no-store"
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post(
    "/mfa/recovery-codes",
    response_model=RecoveryCodesResponse,
    tags=["mfa"],
    summary="New recovery codes, confirmed with an authenticator code or an email code",
)
async def mfa_regenerate_codes(
    body: MfaRegenerateRequest, response: Response, user: CurrentUser, mfa: MfaServiceDep
) -> RecoveryCodesResponse:
    codes = await mfa.regenerate_recovery_codes(user, body.code, body.email_code, body.passkey)
    response.headers["Cache-Control"] = "no-store"
    return RecoveryCodesResponse(recovery_codes=codes)


# --- Passkeys (ADR 0029) --------------------------------------------------------


def _passkey(row: Any) -> PasskeyResponse:
    return PasskeyResponse(
        id=row.id,
        name=row.name,
        backed_up=row.backed_up,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


@router.get("/mfa/passkeys", response_model=list[PasskeyResponse], tags=["mfa"])
async def list_passkeys(user: CurrentUser, mfa: MfaServiceDep) -> list[PasskeyResponse]:
    return [_passkey(p) for p in await mfa.list_passkeys(user)]


@router.post(
    "/mfa/passkeys/options",
    tags=["mfa"],
    summary="Start adding a passkey (requires your password); pass the result to the browser",
)
async def passkey_registration_options(
    body: PasskeyOptionsRequest, user: CurrentUser, mfa: MfaServiceDep
) -> dict[str, Any]:
    return await mfa.passkey_registration_options(user, body.password)


@router.post(
    "/mfa/passkeys",
    status_code=status.HTTP_201_CREATED,
    response_model=PasskeyRegisteredResponse,
    tags=["mfa"],
    summary="Save the passkey the browser created (turns 2FA on if it was off)",
)
async def register_passkey(
    body: PasskeyRegisterRequest, response: Response, user: CurrentUser, mfa: MfaServiceDep
) -> PasskeyRegisteredResponse:
    row, codes = await mfa.register_passkey(user, body.credential, body.name)
    response.headers["Cache-Control"] = "no-store"
    return PasskeyRegisteredResponse(passkey=_passkey(row), recovery_codes=codes)


@router.post(
    "/mfa/passkeys/{passkey_id}/remove",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["mfa"],
    summary="Remove a passkey (requires your password)",
)
async def remove_passkey(
    passkey_id: uuid.UUID, body: PasskeyOptionsRequest, user: CurrentUser, mfa: MfaServiceDep
) -> Response:
    await mfa.remove_passkey(user, passkey_id, body.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
