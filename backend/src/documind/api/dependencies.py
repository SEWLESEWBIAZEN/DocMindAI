"""FastAPI dependency providers. Routes depend on these, never on globals."""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import BackgroundTasks, Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.documents.collection_service import CollectionService
from documind.application.documents.document_service import DocumentService
from documind.application.health_service import HealthService
from documind.application.identity.account_service import AccountService
from documind.application.identity.auth_service import AuthService
from documind.application.identity.common import Principal, RequestMeta
from documind.application.identity.invitation_service import InvitationService
from documind.application.identity.mfa_service import MfaService
from documind.application.identity.workspace_service import WorkspaceService
from documind.application.ports import EmailMessage, EmailSender
from documind.container import Container
from documind.core.config import Settings
from documind.core.logging import get_request_id
from documind.domain.errors import AuthenticationError, PermissionDeniedError
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import User


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def get_settings_dep(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings


def get_health_service(container: Annotated[Container, Depends(get_container)]) -> HealthService:
    return container.health_service


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


_email_logger = logging.getLogger("documind.email")
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")

CSRF_HEADER = "X-CSRF-Protection"


async def get_session(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    async with container.session_factory() as session:
        yield session


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=get_request_id(),
    )


class BackgroundEmailSender:
    """Sends email after the response is returned.

    Keeps SMTP latency off the request (and out of timing side channels such
    as "does this account exist?"), and only runs for committed work because
    services send after commit.
    """

    def __init__(self, tasks: BackgroundTasks, delegate: EmailSender) -> None:
        self._tasks = tasks
        self._delegate = delegate

    async def send(self, message: EmailMessage) -> None:
        self._tasks.add_task(self._deliver, message)

    async def _deliver(self, message: EmailMessage) -> None:
        try:
            await self._delegate.send(message)
        except Exception:
            _email_logger.exception("Email delivery failed", extra={"subject": message.subject})


SessionDep = Annotated[AsyncSession, Depends(get_session)]
MetaDep = Annotated[RequestMeta, Depends(get_request_meta)]
ContainerDep = Annotated[Container, Depends(get_container)]


def get_email_sender(tasks: BackgroundTasks, container: ContainerDep) -> EmailSender:
    return BackgroundEmailSender(tasks, container.email_sender)


EmailDep = Annotated[EmailSender, Depends(get_email_sender)]


def get_auth_service(
    session: SessionDep, container: ContainerDep, email: EmailDep, meta: MetaDep
) -> AuthService:
    return AuthService(
        session,
        container.settings,
        container.password_hasher,
        container.access_tokens,
        email,
        container.rate_limiter,
        meta,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_workspace_service(session: SessionDep, meta: MetaDep) -> WorkspaceService:
    return WorkspaceService(session, meta)


def get_invitation_service(
    session: SessionDep, container: ContainerDep, email: EmailDep, meta: MetaDep
) -> InvitationService:
    return InvitationService(session, container.settings, email, meta)


def get_account_service(
    session: SessionDep,
    container: ContainerDep,
    email: EmailDep,
    meta: MetaDep,
    auth: AuthServiceDep,
) -> AccountService:
    return AccountService(
        session,
        container.settings,
        container.password_hasher,
        email,
        container.rate_limiter,
        auth,
        meta,
    )


def get_mfa_service(
    session: SessionDep, container: ContainerDep, email: EmailDep, meta: MetaDep
) -> MfaService:
    return MfaService(
        session,
        container.settings,
        container.password_hasher,
        container.secret_box,
        email,
        container.rate_limiter,
        meta,
    )


MfaServiceDep = Annotated[MfaService, Depends(get_mfa_service)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(get_workspace_service)]
InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]


async def get_principal(
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Sign in to continue.", code="not_authenticated")
    return await auth.authenticate(credentials.credentials)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_current_user(principal: CurrentPrincipal) -> User:
    return principal.user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_workspace_context(
    workspace_id: uuid.UUID, principal: CurrentPrincipal, workspaces: WorkspaceServiceDep
) -> WorkspaceContext:
    """Authorize the caller for the workspace in the path (404 for non-members)."""
    return await workspaces.resolve_context(
        principal.user, workspace_id, mfa_verified_at=principal.mfa_verified_at
    )


WorkspaceCtx = Annotated[WorkspaceContext, Depends(get_workspace_context)]


def require_csrf_header(
    x_csrf_protection: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """Cookie-authenticated endpoints require a custom header.

    Browsers cannot attach custom headers to cross-site requests without a CORS
    preflight, which this API never approves for this header. Combined with the
    SameSite=Strict cookie, this blocks cross-site request forgery.
    """
    if x_csrf_protection != "1":
        raise PermissionDeniedError("Missing CSRF protection header.", code="csrf_header_missing")


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def get_document_service(
    session: SessionDep, container: ContainerDep, meta: MetaDep
) -> DocumentService:
    return DocumentService(
        session, container.settings, container.storage, container.malware_scanner, meta
    )


def get_collection_service(session: SessionDep, meta: MetaDep) -> CollectionService:
    return CollectionService(session, meta)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
