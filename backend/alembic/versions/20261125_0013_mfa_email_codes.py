"""One-time codes by email as a fallback second factor (ADR 0027).

Schema only: widens the account_tokens purpose check. No rows are changed.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-25 09:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "purpose IN ('email_verification', 'password_reset', 'mfa_login')"
_NEW = "purpose IN ('email_verification', 'password_reset', 'mfa_login', 'mfa_email')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", _NEW)


def downgrade() -> None:
    op.execute("DELETE FROM account_tokens WHERE purpose = 'mfa_email'")
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", _OLD)
