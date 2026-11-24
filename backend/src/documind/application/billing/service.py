"""Plans, entitlements and quotas (ADR 0020)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.domain.billing.plans import (
    DEFAULT_PLAN,
    PLANS,
    BilledTo,
    Capability,
    Entitlements,
    Plan,
    PlanKey,
)
from documind.domain.errors import NotFoundError, QuotaExceededError, ValidationFailedError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import (
    Document,
    DocumentVersion,
    Invitation,
    Workspace,
    WorkspaceMember,
    WorkspacePlan,
)
from documind.infrastructure.database.repositories.billing import (
    UsageRepository,
    WorkspacePlanRepository,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository

#: Monthly limits and the ledger quantity that counts toward them.
AI_METRICS: dict[str, tuple[tuple[Capability, ...], tuple[str, ...]]] = {
    "chat_tokens": ((Capability.CHAT,), ("input_tokens", "output_tokens")),
    "embedding_tokens": ((Capability.EMBEDDING,), ("input_tokens",)),
    "audio_seconds": ((Capability.SPEECH_TO_TEXT, Capability.TEXT_TO_SPEECH), ("audio_seconds",)),
    "figure_descriptions": ((Capability.VISION,), ("units",)),
}

METRIC_LABELS = {
    "chat_tokens": "AI answer tokens",
    "embedding_tokens": "indexing tokens",
    "audio_seconds": "server speech",
    "figure_descriptions": "figure descriptions",
    "max_documents": "documents",
    "storage_bytes": "storage",
    "max_members": "members",
}


def period_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Calendar month in UTC."""
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    metric: str
    label: str
    limit: int | None
    used: float

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.used >= self.limit


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan: Plan
    entitlements: Entitlements
    has_overrides: bool
    period_start: datetime
    period_end: datetime
    quotas: list[QuotaStatus]
    #: Per capability and payer: tokens, seconds, characters, units, calls.
    usage: dict[str, dict[str, float]]


def _resolve_plan(row: WorkspacePlan | None) -> tuple[Plan, Entitlements, bool]:
    key = PlanKey(row.plan_key) if row is not None else DEFAULT_PLAN
    plan = PLANS[key]
    overrides = row.overrides if row is not None else {}
    return plan, plan.entitlements.with_overrides(overrides), bool(overrides)


class BillingService:
    """Control-plane session for plans and usage; data-plane session for document totals."""

    def __init__(
        self,
        session: AsyncSession,
        meta: RequestMeta | None = None,
        *,
        data_session: AsyncSession | None = None,
    ) -> None:
        self._s = session
        self._data = data_session or session
        self._plans = WorkspacePlanRepository(session)
        self._usage = UsageRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta or RequestMeta())

    async def plan(self, workspace_id: uuid.UUID) -> tuple[Plan, Entitlements, bool]:
        return _resolve_plan(await self._plans.get(workspace_id))

    async def entitlements(self, workspace_id: uuid.UUID) -> Entitlements:
        return (await self.plan(workspace_id))[1]

    # ------------------------------------------------------------------ usage

    async def _ai_used(self, workspace_id: uuid.UUID, metric: str, since: datetime) -> float:
        totals = await self._usage.totals(
            workspace_id, since=since, billed_to=BilledTo.PLATFORM.value
        )
        capabilities, fields = AI_METRICS[metric]
        return sum(
            totals.get(f"{c.value}:platform", {}).get(f, 0.0) for c in capabilities for f in fields
        )

    async def document_totals(self, workspace_id: uuid.UUID) -> tuple[int, int]:
        count = (
            await self._data.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.workspace_id == workspace_id)
            )
        ).scalar_one()
        size = (
            await self._data.execute(
                select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
                    DocumentVersion.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        return int(count), int(size)

    async def member_total(self, workspace_id: uuid.UUID) -> int:
        members = (
            await self._s.execute(
                select(func.count())
                .select_from(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == workspace_id)
            )
        ).scalar_one()
        pending = (
            await self._s.execute(
                select(func.count())
                .select_from(Invitation)
                .where(
                    Invitation.workspace_id == workspace_id,
                    Invitation.status == "pending",
                    Invitation.expires_at > utcnow(),
                )
            )
        ).scalar_one()
        return int(members) + int(pending)

    # ----------------------------------------------------------------- quotas

    @staticmethod
    def _raise(metric: str, limit: int, used: float, resets_at: datetime | None) -> None:
        label = METRIC_LABELS[metric]
        when = f" It resets on {resets_at:%B} 1." if resets_at else ""
        raise QuotaExceededError(
            f"This workspace has reached its plan limit for {label}.{when} "
            "An owner can upgrade the plan, or add the workspace's own AI key.",
            metric=metric,
            limit=limit,
            used=used,
            resets_at=resets_at.isoformat() if resets_at else None,
        )

    async def check_ai(self, workspace_id: uuid.UUID, metric: str, ents: Entitlements) -> None:
        """Raise ``QuotaExceededError`` if a platform-billed monthly limit is used up."""
        limit = getattr(ents, metric)
        if limit is None:
            return
        start, end = period_bounds(utcnow())
        used = await self._ai_used(workspace_id, metric, start)
        if used >= limit:
            self._raise(metric, limit, used, end)

    async def remaining_ai(
        self, workspace_id: uuid.UUID, metric: str, ents: Entitlements
    ) -> int | None:
        limit = getattr(ents, metric)
        if limit is None:
            return None
        start, _ = period_bounds(utcnow())
        return max(0, int(limit - await self._ai_used(workspace_id, metric, start)))

    async def check_upload(
        self, workspace_id: uuid.UUID, ents: Entitlements, *, size: int = 0
    ) -> None:
        count, stored = await self.document_totals(workspace_id)
        if ents.max_documents is not None and count >= ents.max_documents:
            self._raise("max_documents", ents.max_documents, count, None)
        if ents.storage_bytes is not None and stored + size > ents.storage_bytes:
            self._raise("storage_bytes", ents.storage_bytes, stored, None)

    async def check_members(self, workspace_id: uuid.UUID, ents: Entitlements) -> None:
        if ents.max_members is None:
            return
        total = await self.member_total(workspace_id)
        if total >= ents.max_members:
            self._raise("max_members", ents.max_members, total, None)

    # ---------------------------------------------------------------- summary

    async def summary(self, ctx: WorkspaceContext) -> PlanSummary:
        ctx.require(Permission.USAGE_READ)
        ws = ctx.workspace_id
        plan, ents, overridden = await self.plan(ws)
        start, end = period_bounds(utcnow())
        usage = await self._usage.totals(ws, since=start)
        count, stored = await self.document_totals(ws)
        quotas = [
            QuotaStatus(m, METRIC_LABELS[m], getattr(ents, m), await self._ai_used(ws, m, start))
            for m in AI_METRICS
        ]
        quotas += [
            QuotaStatus("max_documents", METRIC_LABELS["max_documents"], ents.max_documents, count),
            QuotaStatus(
                "storage_bytes", METRIC_LABELS["storage_bytes"], ents.storage_bytes, stored
            ),
            QuotaStatus(
                "max_members",
                METRIC_LABELS["max_members"],
                ents.max_members,
                await self.member_total(ws),
            ),
        ]
        return PlanSummary(plan, ents, overridden, start, end, quotas, usage)

    # ------------------------------------------------------- operator actions

    async def assign(
        self,
        workspace_id: uuid.UUID,
        plan_key: str,
        *,
        overrides: dict[str, Any] | None,
        operator: str,
        note: str | None = None,
    ) -> WorkspacePlan:
        """Assign a plan (``documind-manage plan assign``). Audited."""
        try:
            key = PlanKey(plan_key)
        except ValueError as exc:
            raise ValidationFailedError(f"Unknown plan '{plan_key}'.") from exc
        if await self._s.get(Workspace, workspace_id) is None:
            raise NotFoundError("Workspace not found.")
        names = set(Entitlements.__dataclass_fields__)
        unknown = set(overrides or {}) - names
        if unknown:
            raise ValidationFailedError(f"Unknown entitlements: {', '.join(sorted(unknown))}.")
        row = await self._plans.get(workspace_id, for_update=True)
        previous = row.plan_key if row else DEFAULT_PLAN.value
        if row is None:
            row = WorkspacePlan(workspace_id=workspace_id, plan_key=key.value)
            self._plans.add(row)
        row.plan_key = key.value
        row.overrides = dict(overrides or {})
        row.assigned_by = operator[:128]
        row.note = note[:255] if note else None
        self._audit.record(
            "workspace.plan_assigned",
            actor_user_id=None,
            workspace_id=workspace_id,
            target_type="workspace",
            target_id=workspace_id,
            details={"from": previous, "to": key.value, "overrides": row.overrides, "by": operator},
        )
        await self._s.commit()
        return row


class CachedEntitlements:
    """``EntitlementSource`` for the AI resolver and workers (short cache)."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, ttl_seconds: float = 10.0
    ) -> None:
        self._sessions = session_factory
        self._ttl = ttl_seconds
        self._cache: dict[uuid.UUID, tuple[float, Entitlements]] = {}

    def invalidate(self, workspace_id: uuid.UUID) -> None:
        self._cache.pop(workspace_id, None)

    async def entitlements(self, workspace_id: uuid.UUID) -> Entitlements:
        now = time.monotonic()
        hit = self._cache.get(workspace_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        async with self._sessions() as session:
            ents = await BillingService(session).entitlements(workspace_id)
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[workspace_id] = (now, ents)
        return ents
