"""Stripe billing: subscriptions and handled webhook events (ADR 0032).

Schema only: two new tables. No existing rows are changed; every workspace keeps
its current plan in ``workspace_plans``.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-25 16:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_subscriptions",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=True),
        sa.Column("plan_key", sa.String(length=32), nullable=True),
        sa.Column("interval", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="none", nullable=False),
        sa.Column("seats", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "interval IS NULL OR interval IN ('month', 'year')",
            name=op.f("ck_billing_subscriptions_interval_valid"),
        ),
        sa.CheckConstraint("seats >= 0", name=op.f("ck_billing_subscriptions_seats_non_negative")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_billing_subscriptions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_billing_subscriptions")),
        sa.UniqueConstraint("customer_id", name=op.f("uq_billing_subscriptions_customer_id")),
        sa.UniqueConstraint(
            "subscription_id", name=op.f("uq_billing_subscriptions_subscription_id")
        ),
    )
    op.create_table(
        "billing_events",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_events")),
    )


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_table("billing_subscriptions")
