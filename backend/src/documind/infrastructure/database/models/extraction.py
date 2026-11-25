"""Structured extraction runs, their cited results, and tracked deadlines (ADR 0034).

These hold document content, so they live in the workspace's data plane. Like
conversations, children reference documents through composite
``(id, workspace_id)`` foreign keys.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)
RUN_STATUSES = ("queued", "running", "completed", "failed")


class ExtractionRun(Base):
    """One extraction over a set of documents with one template."""

    __tablename__ = "extraction_runs"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_extraction_runs_id_workspace_id"),
        CheckConstraint(f"status IN {RUN_STATUSES}", name="status_valid"),
        CheckConstraint("mode IN ('fields', 'items')", name="mode_valid"),
        Index("ix_extraction_runs_workspace_id_created_at", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    template_key: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(8))
    #: ``[{"key", "label", "description", "type"}]``
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    #: Document ids in the order chosen.
    document_ids: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    documents_done: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    #: Per-document notes, e.g. ``{"<id>": "not_ready"}``.
    warnings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


class ExtractionResult(Base):
    """One row: a document's field values (fields mode) or one item (items mode).

    ``values`` maps a field key to ``{"value", "date"?, "chunk_id", "page_start",
    "page_end", "excerpt"}``; every value carries the source it came from.
    """

    __tablename__ = "extraction_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["extraction_runs.id", "extraction_runs.workspace_id"],
            ondelete="CASCADE",
            name="fk_extraction_results_run_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_extraction_results_document_workspace",
        ),
        Index("ix_extraction_results_run_id_position", "run_id", "position"),
        Index("ix_extraction_results_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    position: Mapped[int] = mapped_column(Integer)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Deadline(Base):
    """A date someone wants to be reminded of, usually found by an extraction."""

    __tablename__ = "deadlines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_deadlines_document_workspace",
        ),
        CheckConstraint("status IN ('open', 'done')", name="status_valid"),
        Index("ix_deadlines_workspace_id_due_date", "workspace_id", "due_date"),
        Index("ix_deadlines_status_due_date", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    document_id: Mapped[uuid.UUID | None] = mapped_column()
    title: Mapped[str] = mapped_column(String(300))
    due_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    #: Where it was found: ``{"chunk_id", "page_start", "page_end", "excerpt"}``.
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Users (control-plane ids) who get reminders.
    assignee_user_ids: Mapped[list[str]] = mapped_column(JSONB)
    #: Days before the due date to send a reminder; 0 = on the day.
    remind_days_before: Mapped[list[int]] = mapped_column(JSONB)
    #: Which of those reminders were sent.
    reminders_sent: Mapped[list[int]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(8), default="open", server_default="open")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
