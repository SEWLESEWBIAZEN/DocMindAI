"""Workspace API keys and the Slack app (ADR 0037).

Schema only: new tables api_keys, slack_installations and slack_user_links,
and the credential kind slack_bot_token. No existing rows change.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-26 05:25:26.023978+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_OLD = (
    "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
    "'connector_token', 'sso_client_secret')"
)
_KINDS_NEW = (
    "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
    "'connector_token', 'sso_client_secret', 'slack_bot_token')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_NEW)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_api_keys_created_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_api_keys_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_keys_token_hash")),
    )
    op.create_index("ix_api_keys_workspace", "api_keys", ["workspace_id"], unique=False)
    op.create_table(
        "slack_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("team_name", sa.String(length=200), nullable=True),
        sa.Column("bot_user_id", sa.String(length=32), nullable=True),
        sa.Column("bot_token_credential_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("installed_by_user_id", sa.Uuid(), nullable=True),
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
            "status IN ('active', 'removed')", name=op.f("ck_slack_installations_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["bot_token_credential_id"],
            ["credentials.id"],
            name=op.f("fk_slack_installations_bot_token_credential_id_credentials"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["installed_by_user_id"],
            ["users.id"],
            name=op.f("fk_slack_installations_installed_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_slack_installations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slack_installations")),
        sa.UniqueConstraint("team_id", name=op.f("uq_slack_installations_team_id")),
        sa.UniqueConstraint("workspace_id", name=op.f("uq_slack_installations_workspace_id")),
    )
    op.create_table(
        "slack_user_links",
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("slack_user_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["slack_installations.id"],
            name=op.f("fk_slack_user_links_installation_id_slack_installations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_slack_user_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "installation_id", "slack_user_id", name=op.f("pk_slack_user_links")
        ),
    )
    op.create_index(
        op.f("ix_slack_user_links_user_id"), "slack_user_links", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_slack_user_links_user_id"), table_name="slack_user_links")
    op.drop_table("slack_user_links")
    op.drop_table("slack_installations")
    op.drop_index("ix_api_keys_workspace", table_name="api_keys")
    op.drop_table("api_keys")
    op.execute("DELETE FROM credentials WHERE kind = 'slack_bot_token'")
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_OLD)
