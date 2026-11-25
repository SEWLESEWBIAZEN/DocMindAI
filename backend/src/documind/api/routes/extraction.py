"""Structured extraction, comparison and tracked deadlines (ADR 0034)."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import (
    ContainerDep,
    DeadlineServiceDep,
    ExtractionServiceDep,
    QuotasDep,
    WorkspaceCtx,
    limit_ai_requests,
)
from documind.application.extraction.service import RunView
from documind.infrastructure.database.models import Deadline, ExtractionRun

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["extraction"])


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    type: Literal["text", "date", "money", "number"] = "text"


class TemplateResponse(BaseModel):
    key: str
    name: str
    description: str
    mode: Literal["fields", "items"]
    fields: list[FieldSpec]


class StartExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(description="A template key, or `custom` with `fields`.")
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    collection_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    fields: list[FieldSpec] | None = Field(default=None, max_length=12)
    mode: Literal["fields", "items"] | None = None


class RunResponse(BaseModel):
    id: uuid.UUID
    title: str
    template_key: str
    mode: Literal["fields", "items"]
    fields: list[FieldSpec]
    document_ids: list[uuid.UUID]
    status: Literal["queued", "running", "completed", "failed"]
    documents_done: int
    error_code: str | None
    warnings: dict[str, str] = Field(description="Per document: not_ready, partial, unreadable…")
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


class CitedValue(BaseModel):
    value: str
    date: dt.date | None = None
    chunk_id: uuid.UUID | None = None
    page_start: int | None = None
    page_end: int | None = None
    excerpt: str | None = None


class ResultRow(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    values: dict[str, CitedValue]


class RunDetailResponse(RunResponse):
    documents: dict[str, str] = Field(description="Document id → title (missing: deleted).")
    results: list[ResultRow]


class DeadlineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    due_date: dt.date
    document_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    source: CitedValue | None = None
    assignee_user_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    remind_days_before: list[int] | None = Field(default=None, max_length=6)


class DeadlineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_date: dt.date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    assignee_user_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    remind_days_before: list[int] | None = Field(default=None, max_length=6)
    status: Literal["open", "done"] | None = None


class DeadlineResponse(BaseModel):
    id: uuid.UUID
    title: str
    due_date: dt.date
    status: Literal["open", "done"]
    document_id: uuid.UUID | None
    document_title: str | None
    notes: str | None
    source: dict[str, Any] | None
    assignee_user_ids: list[uuid.UUID]
    remind_days_before: list[int]
    reminders_sent: list[int]
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


def _run(run: ExtractionRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "title": run.title,
        "template_key": run.template_key,
        "mode": run.mode,
        "fields": run.fields,
        "document_ids": run.document_ids,
        "status": run.status,
        "documents_done": run.documents_done,
        "error_code": run.error_code,
        "warnings": run.warnings,
        "created_by_user_id": run.created_by_user_id,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


def _detail(view: RunView) -> RunDetailResponse:
    return RunDetailResponse(
        **_run(view.run),
        documents={str(k): v for k, v in view.titles.items()},
        results=[
            ResultRow(id=r.id, document_id=r.document_id, values=r.values) for r in view.results
        ],
    )


def _deadline(row: Deadline, titles: dict[uuid.UUID, str]) -> DeadlineResponse:
    return DeadlineResponse(
        id=row.id,
        title=row.title,
        due_date=row.due_date,
        status=row.status,
        document_id=row.document_id,
        document_title=titles.get(row.document_id) if row.document_id else None,
        notes=row.notes,
        source=row.source,
        assignee_user_ids=[uuid.UUID(u) for u in row.assignee_user_ids],
        remind_days_before=row.remind_days_before,
        reminders_sent=row.reminders_sent,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.get("/extraction-templates", response_model=list[TemplateResponse])
async def list_templates(
    ctx: WorkspaceCtx, extractions: ExtractionServiceDep
) -> list[TemplateResponse]:
    del ctx
    return [
        TemplateResponse(
            key=t.key,
            name=t.name,
            description=t.description,
            mode=t.mode,
            fields=[FieldSpec(**f.to_dict()) for f in t.fields],
        )
        for t in extractions.templates()
    ]


@router.post(
    "/extractions",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_ai_requests)],
    summary="Extract fields or items from documents, with a source for every value",
)
async def start_extraction(
    body: StartExtractionRequest,
    ctx: WorkspaceCtx,
    extractions: ExtractionServiceDep,
    quotas: QuotasDep,
) -> RunResponse:
    await quotas.answers()  # uses the workspace's chat model and quota
    run = await extractions.start(
        ctx,
        template_key=body.template,
        document_ids=body.document_ids,
        collection_id=body.collection_id,
        title=body.title,
        fields=[f.model_dump() for f in body.fields] if body.fields else None,
        mode=body.mode,
    )
    return RunResponse(**_run(run))


@router.get("/extractions", response_model=list[RunResponse])
async def list_extractions(
    ctx: WorkspaceCtx, extractions: ExtractionServiceDep
) -> list[RunResponse]:
    return [RunResponse(**_run(r)) for r in await extractions.runs(ctx)]


@router.get("/extractions/{run_id}", response_model=RunDetailResponse)
async def get_extraction(
    run_id: uuid.UUID, ctx: WorkspaceCtx, extractions: ExtractionServiceDep
) -> RunDetailResponse:
    return _detail(await extractions.get(ctx, run_id))


@router.get(
    "/extractions/{run_id}/export.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_extraction(
    run_id: uuid.UUID, ctx: WorkspaceCtx, extractions: ExtractionServiceDep
) -> Response:
    body = await extractions.csv(ctx, run_id)
    return Response(
        content="﻿" + body,  # BOM: Excel reads UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="extraction-{run_id}.csv"'},
    )


@router.delete("/extractions/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_extraction(
    run_id: uuid.UUID, ctx: WorkspaceCtx, extractions: ExtractionServiceDep
) -> Response:
    await extractions.delete(ctx, run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/deadlines", response_model=list[DeadlineResponse])
async def list_deadlines(
    ctx: WorkspaceCtx,
    deadlines: DeadlineServiceDep,
    status_filter: Annotated[Literal["open", "done", "all"], Query(alias="status")] = "open",
) -> list[DeadlineResponse]:
    rows = await deadlines.deadlines(ctx, status=status_filter)
    titles = await deadlines.titles(ctx, rows)
    return [_deadline(r, titles) for r in rows]


@router.post("/deadlines", response_model=DeadlineResponse, status_code=status.HTTP_201_CREATED)
async def create_deadline(
    body: DeadlineRequest, ctx: WorkspaceCtx, deadlines: DeadlineServiceDep
) -> DeadlineResponse:
    row = await deadlines.create(
        ctx,
        title=body.title,
        due_date=body.due_date,
        document_id=body.document_id,
        notes=body.notes,
        source=body.source.model_dump(mode="json") if body.source else None,
        assignee_user_ids=body.assignee_user_ids,
        remind_days_before=body.remind_days_before,
    )
    return _deadline(row, await deadlines.titles(ctx, [row]))


@router.patch("/deadlines/{deadline_id}", response_model=DeadlineResponse)
async def update_deadline(
    deadline_id: uuid.UUID, body: DeadlineUpdate, ctx: WorkspaceCtx, deadlines: DeadlineServiceDep
) -> DeadlineResponse:
    row = await deadlines.update(ctx, deadline_id, body.model_dump(exclude_unset=True))
    return _deadline(row, await deadlines.titles(ctx, [row]))


@router.delete("/deadlines/{deadline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deadline(
    deadline_id: uuid.UUID, ctx: WorkspaceCtx, deadlines: DeadlineServiceDep
) -> Response:
    await deadlines.delete(ctx, deadline_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/deadlines/calendar.ics",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}},
    summary="Open deadlines as an iCalendar file",
)
async def deadlines_calendar(
    ctx: WorkspaceCtx, deadlines: DeadlineServiceDep, container: ContainerDep
) -> Response:
    body = await deadlines.calendar(ctx, container.settings.public_app_url)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="documind-deadlines.ics"'},
    )
