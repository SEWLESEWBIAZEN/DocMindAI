"""Per-process runtime for Celery workers.

Each worker process lazily creates one event loop and one container, so
database and Redis connection pools are reused across tasks (and never shared
across forked processes).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Coroutine
from typing import Any

from documind.application.infrastructure.migration import MigrationExecutor
from documind.application.infrastructure.resolver import InfrastructureTargets
from documind.application.infrastructure.sweeps import sweep_processing, sweep_replies
from documind.application.processing.orchestrator import ProcessingOrchestrator
from documind.container import Container, build_container, processing_ai
from documind.core.config import get_settings

_loop: asyncio.AbstractEventLoop | None = None
_container: Container | None = None


def _state() -> tuple[asyncio.AbstractEventLoop, Container]:
    global _loop, _container
    if _loop is None or _container is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _container = build_container(get_settings())
    return _loop, _container


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    loop, _ = _state()
    return loop.run_until_complete(coro)


def container() -> Container:
    return _state()[1]


def orchestrator() -> ProcessingOrchestrator:
    c = container()
    return ProcessingOrchestrator(
        c.session_factory,
        c.settings,
        c.storage,
        c.extractor,
        worker=f"{socket.gethostname()}:{os.getpid()}",
        ai=processing_ai(c),
        targets=InfrastructureTargets(c.infrastructure()),
    )


async def sweep() -> object:
    c = container()
    return await sweep_processing(c.infrastructure(), c.settings, c.job_dispatcher)


async def run_extraction(run_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
    """One extraction run on the workspace's own data plane, with its own AI settings."""
    from documind.application.extraction.runner import ExtractionRunner

    c = container()
    infra = await c.infrastructure().resolve(workspace_id)

    async def llm_for(ws: uuid.UUID, user: uuid.UUID | None) -> Any:
        return (await c.ai_resolver().resolve(ws, user_id=user)).llm

    return await ExtractionRunner(infra.data_plane.sessions, llm_for, c.settings).run(run_id)


async def sweep_extractions() -> int:
    from documind.application.infrastructure.sweeps import sweep_extractions as sweep

    c = container()
    return await sweep(
        c.infrastructure(), c.extraction(), stale_seconds=c.settings.extraction_stale_seconds
    )


async def deadline_reminders() -> int:
    from documind.application.infrastructure.sweeps import send_deadline_reminders

    c = container()
    return await send_deadline_reminders(
        c.infrastructure(), c.session_factory, c.email_sender, c.settings.public_app_url
    )


async def data_export(export_id: uuid.UUID, c: Container | None = None) -> str:
    """Build one "download my data" archive (ADR 0039)."""
    from documind.application.identity.data_export import build_export

    c = c or container()
    return await build_export(
        export_id,
        sessions=c.session_factory,
        settings=c.settings,
        infrastructure=c.infrastructure(),
        storage=c.storage,
        email=c.email_sender,
    )


async def sweep_data_exports(c: Container | None = None) -> int:
    from documind.application.identity.data_export import sweep_exports

    c = c or container()
    return await sweep_exports(c.session_factory, c.storage)


class _NoDispatch:
    async def dispatch(self, job_id: uuid.UUID, **_: Any) -> bool:
        return True


async def connector_data(workspace_id: uuid.UUID) -> Any:
    """The workspace's data plane and a document service on it (connector imports)."""
    from documind.application.connectors.sync import DataAccess
    from documind.application.documents.document_service import DocumentService
    from documind.application.identity.common import RequestMeta

    c = container()
    infra = await c.infrastructure().resolve(workspace_id)
    dispatcher: Any = (
        _NoDispatch() if infra.config.processing.mode == "customer" else c.job_dispatcher
    )
    return DataAccess(
        infra.data_plane.sessions,
        lambda s: DocumentService(
            s, c.settings, infra.storage, c.malware_scanner, RequestMeta(), dispatcher
        ),
    )


async def sync_connector(connection_id: uuid.UUID) -> str:
    from documind.application.connectors.sync import DriveSync

    c = container()
    if c.drive_client is None:
        return "not_configured"
    lock = f"connector-sync:{connection_id}"
    if not await c.redis.set(lock, "1", nx=True, ex=1800):
        return "already_running"
    try:
        report = await DriveSync(
            c.session_factory,
            c.drive_client,
            c.cipher,
            connector_data,
            max_files=c.settings.connector_max_files_per_sync,
            max_bytes=c.settings.max_upload_bytes,
        ).run(connection_id)
        return report.status
    finally:
        await c.redis.delete(lock)


async def schedule_connector_syncs() -> int:
    from datetime import timedelta

    from sqlalchemy import or_, select

    from documind.application.identity.common import utcnow
    from documind.infrastructure.database.models import ConnectorConnection

    c = container()
    if c.drive_client is None:
        return 0
    due = utcnow() - timedelta(seconds=c.settings.connector_sync_interval_seconds)
    async with c.session_factory() as s:
        rows = (
            await s.execute(
                select(ConnectorConnection.id, ConnectorConnection.workspace_id).where(
                    ConnectorConnection.status.in_(("active", "paused")),
                    or_(
                        ConnectorConnection.last_sync_at.is_(None),
                        ConnectorConnection.last_sync_at < due,
                    ),
                )
            )
        ).all()
    for connection_id, workspace_id in rows:
        await c.connectors()(connection_id, workspace_id)
    return len(rows)


async def sync_seats() -> int:
    """Keep Stripe subscription quantities equal to member counts (ADR 0032)."""
    from documind.application.billing.subscriptions import SubscriptionService
    from documind.container import plan_changed

    c = container()
    if c.billing_gateway is None:
        return 0
    async with c.session_factory() as session:
        return await SubscriptionService(
            session,
            c.settings,
            c.billing_gateway,
            email=c.email_sender,
            on_plan_change=plan_changed(c),
        ).sync_seats()


async def sweep_replies_all() -> int:
    c = container()
    return await sweep_replies(
        c.infrastructure(), stale_after_seconds=c.settings.chat_stream_stale_seconds
    )


async def run_migration(migration_id: uuid.UUID) -> dict[str, object]:
    c = container()
    executor = MigrationExecutor(c.session_factory, c.infrastructure())
    return (await executor.run(migration_id)).as_dict()


async def slack_answer(job: dict[str, Any], c: Container | None = None) -> str:
    """Answer one Slack question and post the reply (ADR 0037)."""
    from documind.application.billing.service import BillingService
    from documind.application.identity.common import RequestMeta
    from documind.application.identity.workspace_service import WorkspaceService
    from documind.application.integrations.slack import AnswerDeps, answer_job
    from documind.application.rag import factory as rag_factory
    from documind.domain.billing.plans import Capability
    from documind.domain.errors import NotFoundError
    from documind.domain.identity.policies import WorkspaceContext
    from documind.infrastructure.database.models import User

    c = c or container()
    if c.slack_client is None:
        return "not_configured"
    async with c.session_factory() as session:

        async def context_for(user_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkspaceContext:
            user = await session.get(User, user_id)
            if user is None or not user.is_active:
                raise NotFoundError("Your linked DocuMind account is no longer active.")
            return await WorkspaceService(
                session,
                RequestMeta(),
                mfa_grace_days=c.settings.mfa_enforcement_grace_days,
                mfa_step_up_seconds=c.settings.mfa_step_up_ttl_seconds,
            ).resolve_context(user, workspace_id)

        async def answer(ctx: WorkspaceContext, question: str) -> Any:
            ai = await c.ai_resolver().resolve(ctx.workspace_id, user_id=ctx.user_id)
            infra = await c.infrastructure().resolve(ctx.workspace_id)
            async with infra.data_plane.sessions() as data:
                if ai.uses_platform(Capability.CHAT):
                    await BillingService(session, RequestMeta(), data_session=data).check_ai(
                        ctx.workspace_id, "chat_tokens", ai.entitlements
                    )
                retrieval = rag_factory.retrieval_service(data, c.settings, ai)
                orchestrator = rag_factory.rag_orchestrator(retrieval, c.settings, ai)
                return await orchestrator.answer(ctx, question)

        return await answer_job(
            job,
            AnswerDeps(
                session=session,
                settings=c.settings,
                client=c.slack_client,
                cipher=c.cipher,
                context_for=context_for,
                answer=answer,
            ),
        )
