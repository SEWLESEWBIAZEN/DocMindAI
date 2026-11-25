"""2FA at sign-in and per-workspace 2FA enforcement (ADR 0026).

Schema only: existing rows are not rewritten. ``mfa_required`` defaults to false
and ``mfa_enforced_since`` starts empty; it is set the first time a workspace is
used while 2FA is required, which starts that workspace's grace period.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-24 19:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "purpose IN ('email_verification', 'password_reset')"
_NEW = "purpose IN ('email_verification', 'password_reset', 'mfa_login')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", _NEW)
    op.add_column(
        "workspaces",
        sa.Column("mfa_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "workspaces", sa.Column("mfa_enforced_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("workspaces", "mfa_enforced_since")
    op.drop_column("workspaces", "mfa_required")
    op.execute("DELETE FROM account_tokens WHERE purpose = 'mfa_login'")
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", _OLD)
