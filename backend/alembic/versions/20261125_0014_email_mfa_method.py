"""Email-only 2FA and the per-organization email-code setting (ADR 0028).

Schema only: ``users.mfa_email_enabled_at`` starts empty and
``workspaces.allow_email_mfa`` defaults to true (today's behaviour). No rows
are rewritten.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-25 12:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_email_enabled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "workspaces",
        sa.Column("allow_email_mfa", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "allow_email_mfa")
    op.drop_column("users", "mfa_email_enabled_at")
