"""Workspace usage summary (admins and owners)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from documind.api.dependencies import SessionDep, WorkspaceCtx
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


def get_usage_queries(session: SessionDep) -> UsageQueries:
    return UsageQueries(session)


@router.get(
    "/usage",
    response_model=UsageSummaryResponse,
    summary="Usage summary for the period (requires usage.read)",
    description=(
        "Documents and storage are current totals; processing and AI figures cover the last "
        "`days` days (UTC). Computed from operational data; one-shot `/ask` calls are not "
        "recorded yet (persisted usage records arrive in Phase 8)."
    ),
)
async def usage_summary(
    ctx: WorkspaceCtx,
    queries: Annotated[UsageQueries, Depends(get_usage_queries)],
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> UsageSummaryResponse:
    return UsageSummaryResponse.model_validate(asdict(await queries.summary(ctx, days=days)))
