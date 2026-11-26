"""Organization SSO and SCIM, and per-collection access (ADR 0036).

Schema only: new tables sso_connections and collection_members, a nullable
refresh_tokens.sso_workspace_id, document_collections.restricted (default false:
every existing collection stays open), and the credential kind
sso_client_secret. No existing rows change.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-26 04:46:06.884025+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KINDS_OLD = (
    "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
    "'connector_token')"
)
_KINDS_NEW = (
    "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
    "'connector_token', 'sso_client_secret')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_NEW)
    op.create_table(
        "collection_members",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            name="fk_collection_members_collection_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_collection_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("collection_id", "user_id", name=op.f("pk_collection_members")),
    )
    op.create_index(
        "ix_collection_members_user_id", "collection_members", ["user_id"], unique=False
    )
    op.create_table(
        "sso_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("client_id", sa.String(length=300), nullable=False),
        sa.Column("client_secret_credential_id", sa.Uuid(), nullable=True),
        sa.Column("domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_role", sa.String(length=16), server_default="member", nullable=False),
        sa.Column("enforced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("verified_by", sa.String(length=128), nullable=True),
        sa.Column("verification_note", sa.String(length=500), nullable=True),
        sa.Column("scim_token_hash", sa.String(length=64), nullable=True),
        sa.Column("scim_token_prefix", sa.String(length=16), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
            "default_role IN ('member', 'viewer')",
            name=op.f("ck_sso_connections_default_role_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'active', 'disabled')",
            name=op.f("ck_sso_connections_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["client_secret_credential_id"],
            ["credentials.id"],
            name=op.f("fk_sso_connections_client_secret_credential_id_credentials"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_sso_connections_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sso_connections_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sso_connections")),
        sa.UniqueConstraint("scim_token_hash", name=op.f("uq_sso_connections_scim_token_hash")),
        sa.UniqueConstraint("workspace_id", name=op.f("uq_sso_connections_workspace_id")),
    )
    op.add_column(
        "document_collections",
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("refresh_tokens", sa.Column("sso_workspace_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM credentials WHERE kind = 'sso_client_secret'")
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_OLD)
    op.drop_column("refresh_tokens", "sso_workspace_id")
    op.drop_column("document_collections", "restricted")
    op.drop_table("sso_connections")
    op.drop_index("ix_collection_members_user_id", table_name="collection_members")
    op.drop_table("collection_members")
