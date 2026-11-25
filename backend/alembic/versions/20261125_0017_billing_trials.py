"""Free trials: who started one and when it ends (ADR 0033).

Schema only: two nullable columns on billing_subscriptions. No rows change.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25 18:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("billing_subscriptions", sa.Column("trial_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "billing_subscriptions",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("billing_subscriptions", "trial_ends_at")
    op.drop_column("billing_subscriptions", "trial_user_id")
