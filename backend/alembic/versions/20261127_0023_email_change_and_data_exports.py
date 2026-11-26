"""Email change and data exports (ADR 0039).

Schema only: account_tokens gains a nullable new_email column and the purpose
'email_change'; a new table data_exports records "download my data" archives.
No existing rows change.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-27 12:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSES_OLD = (
    "purpose IN ('email_verification', 'password_reset', 'mfa_login', 'mfa_email', "
    "'webauthn_reg', 'webauthn_auth')"
)
_PURPOSES_NEW = (
    "purpose IN ('email_verification', 'password_reset', 'mfa_login', 'mfa_email', "
    "'webauthn_reg', 'webauthn_auth', 'email_change')"
)


def upgrade() -> None:
    op.add_column("account_tokens", sa.Column("new_email", sa.String(length=320), nullable=True))
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(
        op.f("ck_account_tokens_purpose_valid"), "account_tokens", _PURPOSES_NEW
    )
    op.create_table(
        "data_exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("object_key", sa.String(length=300), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed', 'expired')",
            name=op.f("ck_data_exports_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_data_exports_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_exports")),
    )
    op.create_index(
        "ix_data_exports_user_id_created_at", "data_exports", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_data_exports_status_expires_at", "data_exports", ["status", "expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_data_exports_status_expires_at", table_name="data_exports")
    op.drop_index("ix_data_exports_user_id_created_at", table_name="data_exports")
    op.drop_table("data_exports")
    op.execute("DELETE FROM account_tokens WHERE purpose = 'email_change'")
    op.drop_constraint(op.f("ck_account_tokens_purpose_valid"), "account_tokens", type_="check")
    op.create_check_constraint(
        op.f("ck_account_tokens_purpose_valid"), "account_tokens", _PURPOSES_OLD
    )
    op.drop_column("account_tokens", "new_email")
