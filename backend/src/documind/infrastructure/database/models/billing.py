"""Plans, workspace subscriptions and the usage ledger (ADR 0020). Control plane."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


class PlanRecord(Base):
    __tablename__ = "plans"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255))
    entitlements: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class WorkspacePlan(Base):
    """One plan per workspace. No row means the default plan (Free)."""

    __tablename__ = "workspace_plans"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    plan_key: Mapped[str] = mapped_column(ForeignKey("plans.key", ondelete="RESTRICT"))
    overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    assigned_by: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(String(255))
    assigned_at: Mapped[datetime] = mapped_column(
        TZ, server_default=func.now(), onupdate=func.now()
    )


class UsageRecord(Base):
    """Append-only ledger of metered provider calls. Holds no document content.

    ``workspace_id`` has no foreign key: the ledger outlives the workspace for billing.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('embedding', 'chat', 'vision', 'speech_to_text', 'text_to_speech')",
            name="capability_valid",
        ),
        CheckConstraint("billed_to IN ('platform', 'workspace_key')", name="billed_to_valid"),
        Index(
            "ix_usage_records_workspace_capability_time",
            "workspace_id",
            "capability",
            "billed_to",
            "occurred_at",
        ),
        Index("ix_usage_records_workspace_id_occurred_at", "workspace_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    capability: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    billed_to: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    characters: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_type: Mapped[str | None] = mapped_column(String(32))
    source_id: Mapped[str | None] = mapped_column(String(64))
    credential_id: Mapped[uuid.UUID | None] = mapped_column()
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class BillingSubscription(Base):
    """A workspace's Stripe customer and subscription (ADR 0032). Holds no card data.

    The plan itself stays in ``workspace_plans``; this row mirrors Stripe's view so
    the app can show status and seats, and map webhooks back to the workspace.
    """

    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        CheckConstraint("interval IS NULL OR interval IN ('month', 'year')", name="interval_valid"),
        CheckConstraint("seats >= 0", name="seats_non_negative"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[str] = mapped_column(String(64), unique=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    plan_key: Mapped[str | None] = mapped_column(String(32))
    interval: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default="none", server_default="none")
    seats: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_period_end: Mapped[datetime | None] = mapped_column(TZ)
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    #: Who started the free trial checkout, and when the trial ends(ed) (ADR 0033).
    trial_user_id: Mapped[uuid.UUID | None] = mapped_column()
    trial_ends_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class BillingEvent(Base):
    """Stripe webhook events already handled: a redelivered event is a no-op."""

    __tablename__ = "billing_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
