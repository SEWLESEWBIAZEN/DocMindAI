"""Per-workspace voice policy (ADR 0011).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24 03:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("voice_policy", sa.String(length=16), server_default="enabled", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_workspaces_voice_policy_valid"),
        "workspaces",
        "voice_policy IN ('enabled', 'browser', 'disabled')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workspaces_voice_policy_valid"), "workspaces", type_="check")
    op.drop_column("workspaces", "voice_policy")
