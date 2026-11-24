"""Workspace usage summary (admins and owners)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from documind.api.dependencies import BillingServiceDep, DataSessionDep, SessionDep, WorkspaceCtx
from documind.application.usage.summary import UsageQueries

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["usage"])


class DocumentUsageResponse(BaseModel):
    total: int
    ready: int
    processing: int
    failed: int
    archived: int
    storage_bytes: int
    pages: int
    chunks: int


class ProcessingUsageResponse(BaseModel):
    jobs_succeeded: int
    jobs_failed: int
    embedding_tokens: int


class AiUsageResponse(BaseModel):
    conversations: int
    questions: int
    answers_completed: int
    answers_failed: int
    answers_cancelled: int
    llm_input_tokens: int
    llm_output_tokens: int
    query_embedding_tokens: int


class DailyUsageResponse(BaseModel):
    day: date
    questions: int
    llm_input_tokens: int
    llm_output_tokens: int
    documents_processed: int


class MemberUsageResponse(BaseModel):
    user_id: uuid.UUID
    display_name: str
    questions: int
    llm_tokens: int


class UsageSummaryResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    documents: DocumentUsageResponse
    processing: ProcessingUsageResponse
    ai: AiUsageResponse
    daily: list[DailyUsageResponse]
    members: list[MemberUsageResponse]


def get_usage_queries(session: SessionDep, data: DataSessionDep) -> UsageQueries:
    return UsageQueries(session, data_session=data)


@router.get(
    "/usage",
    response_model=UsageSummaryResponse,
    summary="Usage summary for the period (requires usage.read)",
    description=(
        "Documents and storage are current totals; processing and AI figures cover the last "
        "`days` days (UTC), computed from operational data. Metered, billable usage "
        "(including `/ask`, speech and figure descriptions) is in `GET /plan`."
    ),
)
async def usage_summary(
    ctx: WorkspaceCtx,
    queries: Annotated[UsageQueries, Depends(get_usage_queries)],
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> UsageSummaryResponse:
    return UsageSummaryResponse.model_validate(asdict(await queries.summary(ctx, days=days)))


# --------------------------------------------------------------------------- #
# Plan, quotas and metered usage (ADR 0020)
# --------------------------------------------------------------------------- #


class EntitlementsResponse(BaseModel):
    max_members: int | None
    max_documents: int | None
    storage_bytes: int | None
    chat_tokens: int | None
    embedding_tokens: int | None
    audio_seconds: int | None
    figure_descriptions: int | None
    byok: bool
    platform_ai: bool
    server_voice: bool
    custom_storage: bool
    dedicated_database: bool
    external_database: bool
    customer_workers: bool


class QuotaResponse(BaseModel):
    metric: str
    label: str
    limit: int | None = Field(description="None means unlimited")
    used: float
    exceeded: bool


class MeteredUsageResponse(BaseModel):
    capability: str
    billed_to: str = Field(description='"platform" or "workspace_key" (your own AI key)')
    calls: int
    input_tokens: int
    output_tokens: int
    audio_seconds: float
    characters: int
    units: int


class PlanSummaryResponse(BaseModel):
    plan_key: str
    plan_name: str
    plan_description: str
    has_overrides: bool
    period_start: datetime
    period_end: datetime
    entitlements: EntitlementsResponse
    quotas: list[QuotaResponse]
    usage: list[MeteredUsageResponse]


@router.get(
    "/plan",
    response_model=PlanSummaryResponse,
    summary="Plan, quotas and metered usage this month (requires usage.read)",
    description=(
        "Monthly quotas count only platform-billed usage; calls made with the workspace's own "
        "AI key are listed with `billed_to: workspace_key` and never count toward them."
    ),
)
async def plan_summary(ctx: WorkspaceCtx, billing: BillingServiceDep) -> PlanSummaryResponse:
    summary = await billing.summary(ctx)
    usage = []
    for key, values in sorted(summary.usage.items()):
        capability, billed_to = key.split(":")
        usage.append(
            MeteredUsageResponse(
                capability=capability,
                billed_to=billed_to,
                calls=int(values["calls"]),
                input_tokens=int(values["input_tokens"]),
                output_tokens=int(values["output_tokens"]),
                audio_seconds=round(values["audio_seconds"], 1),
                characters=int(values["characters"]),
                units=int(values["units"]),
            )
        )
    return PlanSummaryResponse(
        plan_key=summary.plan.key.value,
        plan_name=summary.plan.name,
        plan_description=summary.plan.description,
        has_overrides=summary.has_overrides,
        period_start=summary.period_start,
        period_end=summary.period_end,
        entitlements=EntitlementsResponse(**summary.entitlements.to_dict()),
        quotas=[
            QuotaResponse(
                metric=q.metric, label=q.label, limit=q.limit, used=q.used, exceeded=q.exceeded
            )
            for q in summary.quotas
        ],
        usage=usage,
    )
