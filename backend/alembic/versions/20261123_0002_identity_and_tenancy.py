"""Identity and tenancy: users, organizations, workspaces, roles, tokens, audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23

Seeds the system roles and permissions. The seed is frozen literal data (not
imported from application code) so this migration means the same thing
forever; later catalog changes get their own migration. A test asserts the
database matches ``documind.domain.identity.permissions``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_actor_user_id_created_at",
        "audit_logs",
        ["actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_workspace_id_created_at",
        "audit_logs",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("key", name=op.f("uq_permissions_key")),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("key", name=op.f("uq_roles_key")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auth_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("default_workspace_id", sa.Uuid(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        # FK to workspaces is added below, once that table exists (circular reference).
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "account_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name=op.f("ck_account_tokens_purpose_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_account_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_account_tokens_token_hash")),
    )
    op.create_index(
        "ix_account_tokens_user_id_purpose", "account_tokens", ["user_id", "purpose"], unique=False
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_organizations_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"], unique=False)
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("permission_id", sa.SmallInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("personal_owner_id", sa.Uuid(), nullable=True),
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
            "(kind = 'personal' AND personal_owner_id IS NOT NULL AND organization_id IS NULL)"
            " OR (kind = 'organization' AND organization_id IS NOT NULL"
            " AND personal_owner_id IS NULL)",
            name=op.f("ck_workspaces_kind_owner_consistent"),
        ),
        sa.CheckConstraint(
            "kind IN ('personal', 'organization')", name=op.f("ck_workspaces_kind_valid")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_workspaces_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["personal_owner_id"],
            ["users.id"],
            name=op.f("fk_workspaces_personal_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_workspaces_organization_id")),
        sa.UniqueConstraint("personal_owner_id", name=op.f("uq_workspaces_personal_owner_id")),
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked')",
            name=op.f("ck_invitations_status_valid"),
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_invitations_email_lowercase")),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_accepted_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_invited_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_invitations_role_id_roles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_invitations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    op.create_index("ix_invitations_email", "invitations", ["email"], unique=False)
    op.create_index(
        "uq_invitations_pending_workspace_email",
        "invitations",
        ["workspace_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_workspace_members_role_id_roles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_members")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name=op.f("uq_workspace_members_workspace_id_user_id")
        ),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_users_default_workspace_id_workspaces"),
        "users",
        "workspaces",
        ["default_workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _seed_roles_and_permissions()


# Frozen seed data. Do not edit: add a new migration to change the catalog.
_PERMISSIONS = [
    (1, "workspace.read", "View the workspace and its settings"),
    (2, "workspace.update", "Change workspace settings"),
    (3, "members.read", "View workspace members"),
    (4, "members.invite", "Invite people and manage pending invitations"),
    (5, "members.manage", "Change member roles and remove members"),
    (6, "audit.read", "View the audit log"),
    (7, "usage.read", "View usage and quotas"),
    (8, "documents.read", "View and download documents"),
    (9, "documents.write", "Upload and edit documents and collections"),
    (10, "documents.delete", "Delete documents and collections"),
    (11, "conversations.use", "Ask questions and hold conversations"),
    (12, "billing.manage", "Manage plan and billing"),
]
_ROLES = [
    (1, "owner", "Owner", "Full control, including billing and ownership", 40),
    (2, "admin", "Admin", "Manages members, settings and all content", 30),
    (3, "member", "Member", "Works with documents and conversations", 20),
    (4, "viewer", "Viewer", "Reads documents and asks questions", 10),
]
_VIEWER = [1, 3, 8, 11]
_MEMBER = [*_VIEWER, 9, 10]
_ADMIN = [*_MEMBER, 2, 4, 5, 6, 7]
_OWNER = [p[0] for p in _PERMISSIONS]
_ROLE_PERMISSIONS = {1: _OWNER, 2: _ADMIN, 3: _MEMBER, 4: _VIEWER}


def _seed_roles_and_permissions() -> None:
    permissions = sa.table(
        "permissions", sa.column("id"), sa.column("key"), sa.column("description")
    )
    roles = sa.table(
        "roles",
        sa.column("id"),
        sa.column("key"),
        sa.column("name"),
        sa.column("description"),
        sa.column("rank"),
    )
    role_permissions = sa.table(
        "role_permissions", sa.column("role_id"), sa.column("permission_id")
    )
    op.bulk_insert(permissions, [{"id": i, "key": k, "description": d} for i, k, d in _PERMISSIONS])
    op.bulk_insert(
        roles,
        [{"id": i, "key": k, "name": n, "description": d, "rank": r} for i, k, n, d, r in _ROLES],
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_id, "permission_id": pid}
            for role_id, pids in _ROLE_PERMISSIONS.items()
            for pid in sorted(set(pids))
        ],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_users_default_workspace_id_workspaces"), "users", type_="foreignkey"
    )
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index(
        "uq_invitations_pending_workspace_email",
        table_name="invitations",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_invitations_email", table_name="invitations")
    op.drop_table("invitations")
    op.drop_table("workspaces")
    op.drop_table("role_permissions")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("organizations")
    op.drop_index("ix_account_tokens_user_id_purpose", table_name="account_tokens")
    op.drop_table("account_tokens")
    op.drop_table("users")
    op.drop_table("roles")
    op.drop_table("permissions")
    op.drop_index("ix_audit_logs_workspace_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
