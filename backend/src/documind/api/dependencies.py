"""FastAPI dependency providers. Routes depend on these, never on globals."""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import BackgroundTasks, Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.ai.resolver import WorkspaceAI
from documind.application.billing.service import BillingService
from documind.application.conversations.finalizer import ReplyFinalizer
from documind.application.conversations.service import ConversationService
from documind.application.conversations.streaming import ReplyRunner, StreamOptions
from documind.application.documents.collection_service import CollectionService
from documind.application.documents.document_service import DocumentService
from documind.application.health_service import HealthService
from documind.application.identity.account_service import AccountService
from documind.application.identity.auth_service import AuthService
from documind.application.identity.common import Principal, RequestMeta
from documind.application.identity.invitation_service import InvitationService
from documind.application.identity.mfa_service import MfaService
from documind.application.identity.workspace_service import WorkspaceService
from documind.application.infrastructure.resolver import ResolvedInfrastructure
from documind.application.ports import EmailMessage, EmailSender
from documind.application.processing.queries import ProcessingQueries
from documind.application.rag.condense import QueryCondenser
from documind.application.rag.orchestrator import GenerationOptions, RAGOrchestrator
from documind.application.rag.retrieval import RetrievalOptions, RetrievalService
from documind.container import Container
from documind.core.config import Settings
from documind.core.logging import get_request_id
from documind.domain.billing.plans import Capability
from documind.domain.errors import (
    AuthenticationError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.dataplane_runtime import relay_outbox
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
# Data planes (ADR 0021)
# --------------------------------------------------------------------------- #


async def get_infrastructure(ctx: WorkspaceCtx, container: ContainerDep) -> ResolvedInfrastructure:
    """Where this workspace's data lives. Resolved only from the authorized workspace."""
    return await container.infrastructure().resolve(ctx.workspace_id)


InfrastructureDep = Annotated[ResolvedInfrastructure, Depends(get_infrastructure)]


async def get_data_session(
    infra: InfrastructureDep, session: SessionDep, container: ContainerDep
) -> AsyncIterator[AsyncSession]:
    """The workspace's data-plane session.

    Shared data plane: the request's own session (one transaction with audit
    entries, exactly as before). Dedicated or external: a session on that
    database; audit entries go to its outbox and are relayed afterwards.
    """
    if not infra.isolated:
        yield session
        return
    async with infra.data_plane.sessions() as data:
        try:
            # Connect now, so an unreachable customer database is a clear 503 for
            # this organization instead of an error halfway through a request.
            await data.connection()
        except (OSError, TimeoutError, DBAPIError) as exc:
            logging.getLogger(__name__).warning(
                "data plane unreachable",
                extra={"workspace_id": str(infra.workspace_id), "error": type(exc).__name__},
            )
            raise ServiceUnavailableError(
                "This organization's database can't be reached right now. Try again shortly; "
                "if it persists, ask an owner to check the infrastructure settings.",
                code="data_plane_unavailable",
            ) from exc
        yield data
    try:
        await relay_outbox(infra.data_plane.sessions, container.session_factory)
    except Exception:  # the beat sweep relays anything left behind
        logging.getLogger(__name__).exception("audit outbox relay failed")


DataSessionDep = Annotated[AsyncSession, Depends(get_data_session)]


# --------------------------------------------------------------------------- #
# AI providers, plans and quotas (ADR 0019, 0020)
# --------------------------------------------------------------------------- #


async def get_workspace_ai(ctx: WorkspaceCtx, container: ContainerDep) -> WorkspaceAI:
    """The workspace's AI adapters (its own key or the platform's), metered."""
    return await container.ai_resolver().resolve(ctx.workspace_id, user_id=ctx.user_id)


WorkspaceAIDep = Annotated[WorkspaceAI, Depends(get_workspace_ai)]


def get_billing_service(session: SessionDep, data: DataSessionDep, meta: MetaDep) -> BillingService:
    return BillingService(session, meta, data_session=data)


BillingServiceDep = Annotated[BillingService, Depends(get_billing_service)]


class Quotas:
    """Admission checks against the workspace's plan. Only platform-billed AI counts."""

    def __init__(self, ctx: WorkspaceContext, ai: WorkspaceAI, billing: BillingService) -> None:
        self._ctx = ctx
        self._ai = ai
        self._billing = billing

    async def answers(self) -> None:
        if self._ai.uses_platform(Capability.CHAT):
            await self._billing.check_ai(
                self._ctx.workspace_id, "chat_tokens", self._ai.entitlements
            )

    async def speech(self) -> None:
        if self._ai.uses_platform(Capability.SPEECH_TO_TEXT) or self._ai.uses_platform(
            Capability.TEXT_TO_SPEECH
        ):
            await self._billing.check_ai(
                self._ctx.workspace_id, "audio_seconds", self._ai.entitlements
            )

    async def upload(self, size: int | None) -> None:
        await self._billing.check_upload(
            self._ctx.workspace_id, self._ai.entitlements, size=size or 0
        )
        if self._ai.uses_platform(Capability.EMBEDDING):
            await self._billing.check_ai(
                self._ctx.workspace_id, "embedding_tokens", self._ai.entitlements
            )

    async def members(self) -> None:
        await self._billing.check_members(self._ctx.workspace_id, self._ai.entitlements)


def get_quotas(ctx: WorkspaceCtx, ai: WorkspaceAIDep, billing: BillingServiceDep) -> Quotas:
    return Quotas(ctx, ai, billing)


QuotasDep = Annotated[Quotas, Depends(get_quotas)]


async def admit_answer(quotas: QuotasDep) -> None:
    await quotas.answers()


async def admit_speech(quotas: QuotasDep) -> None:
    await quotas.speech()


async def admit_member(quotas: QuotasDep) -> None:
    await quotas.members()


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def get_document_service(
    session: DataSessionDep, container: ContainerDep, meta: MetaDep, infra: InfrastructureDep
) -> DocumentService:
    return DocumentService(
        session,
        container.settings,
        infra.storage,
        container.malware_scanner,
        meta,
        container.job_dispatcher,
    )


def get_collection_service(session: DataSessionDep, meta: MetaDep) -> CollectionService:
    return CollectionService(session, meta)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]


def get_processing_queries(session: DataSessionDep) -> ProcessingQueries:
    return ProcessingQueries(session)


ProcessingQueriesDep = Annotated[ProcessingQueries, Depends(get_processing_queries)]


# --------------------------------------------------------------------------- #
# Search and answers (RAG)
# --------------------------------------------------------------------------- #


def get_retrieval_service(
    session: DataSessionDep, container: ContainerDep, ai: WorkspaceAIDep
) -> RetrievalService:
    s = container.settings
    return RetrievalService(
        session,
        ai.embeddings,
        RetrievalOptions(
            top_k=s.retrieval_top_k,
            candidates=s.retrieval_candidates,
            min_similarity=s.retrieval_min_score,
            hybrid=s.retrieval_hybrid,
            ef_search=s.retrieval_ef_search,
        ),
    )


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]


def get_rag_orchestrator(
    retrieval: RetrievalServiceDep, container: ContainerDep, ai: WorkspaceAIDep
) -> RAGOrchestrator:
    s = container.settings
    return RAGOrchestrator(
        retrieval,
        ai.llm,
        GenerationOptions(
            max_output_tokens=s.llm_max_output_tokens,
            temperature=s.llm_temperature,
            context_max_tokens=s.rag_context_max_tokens,
            history_max_turns=s.conversation_history_turns,
            history_max_tokens=s.conversation_history_max_tokens,
        ),
        condenser=QueryCondenser(
            ai.llm,
            enabled=s.conversation_condense_followups,
            temperature=s.llm_temperature,
        ),
    )


RAGOrchestratorDep = Annotated[RAGOrchestrator, Depends(get_rag_orchestrator)]


async def limit_ai_requests(ctx: WorkspaceCtx, container: ContainerDep) -> None:
    """Per-user budget for embedding/LLM-backed requests (search and answers)."""
    s = container.settings
    await container.rate_limiter.hit(
        "ai:user", str(ctx.user_id), s.rate_limit_ai_per_user, s.rate_limit_ai_window_seconds
    )


async def limit_outbound_checks(ctx: WorkspaceCtx, container: ContainerDep) -> None:
    """Key and infrastructure validation connect to customer-chosen hosts: cap them."""
    s = container.settings
    await container.rate_limiter.hit(
        "outbound:workspace",
        str(ctx.workspace_id),
        s.rate_limit_outbound_checks_per_workspace,
        s.rate_limit_outbound_checks_window_seconds,
    )


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #


def get_conversation_service(
    session: DataSessionDep,
    container: ContainerDep,
    meta: MetaDep,
    orchestrator: RAGOrchestratorDep,
    infra: InfrastructureDep,
) -> ConversationService:
    s = container.settings
    runner = ReplyRunner(
        orchestrator,
        ReplyFinalizer(infra.data_plane.sessions),
        container.cancellation,
        StreamOptions(
            heartbeat_seconds=s.chat_stream_heartbeat_seconds,
            cancel_poll_seconds=s.chat_cancel_poll_seconds,
            touch_seconds=max(5.0, s.chat_stream_stale_seconds / 6),
        ),
    )
    return ConversationService(
        session,
        meta,
        orchestrator=orchestrator,
        runner=runner,
        cancellation=container.cancellation,
        stale_after_seconds=s.chat_stream_stale_seconds,
    )


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
