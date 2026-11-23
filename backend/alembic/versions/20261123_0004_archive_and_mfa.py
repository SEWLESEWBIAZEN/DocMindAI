"""Document archiving and two-factor authentication.

Members lose documents.delete and gain documents.archive_own; admins and
owners gain documents.archive_own and documents.archive_any. Seed changes
are frozen literal data (see 0002).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23 16:41:29.569539+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mfa_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_recovery_codes")),
        sa.UniqueConstraint(
            "user_id", "code_hash", name=op.f("uq_mfa_recovery_codes_user_id_code_hash")
        ),
    )
    op.add_column("documents", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("archived_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_documents_archived_by_user_id_users"),
        "documents",
        "users",
        ["archived_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("users", sa.Column("totp_secret_encrypted", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_last_used_step", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("totp_pending_secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("totp_pending_expires_at", sa.DateTime(timezone=True), nullable=True)
    )

    _upgrade_permissions()


def downgrade() -> None:
    _downgrade_permissions()
    op.drop_column("users", "totp_pending_expires_at")
    op.drop_column("users", "totp_pending_secret_encrypted")
    op.drop_column("users", "totp_last_used_step")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret_encrypted")
    op.drop_constraint(
        op.f("fk_documents_archived_by_user_id_users"), "documents", type_="foreignkey"
    )
    op.drop_column("documents", "archived_by_user_id")
    op.drop_column("documents", "archived_at")
    op.drop_table("mfa_recovery_codes")


# Frozen data. Role ids: 1 owner, 2 admin, 3 member, 4 viewer. Permission 10 = documents.delete.
_ARCHIVE_OWN, _ARCHIVE_ANY, _DELETE = 13, 14, 10
_MEMBER, _ADMIN, _OWNER = 3, 2, 1

_permissions = sa.table("permissions", sa.column("id"), sa.column("key"), sa.column("description"))
_role_permissions = sa.table("role_permissions", sa.column("role_id"), sa.column("permission_id"))


def _set_delete_description(text: str) -> None:
    op.execute(sa.update(_permissions).where(_permissions.c.id == _DELETE).values(description=text))


def _upgrade_permissions() -> None:
    op.bulk_insert(
        _permissions,
        [
            {
                "id": _ARCHIVE_OWN,
                "key": "documents.archive_own",
                "description": "Archive and restore documents you uploaded",
            },
            {
                "id": _ARCHIVE_ANY,
                "key": "documents.archive_any",
                "description": "Archive and restore any document",
            },
        ],
    )
    _set_delete_description("Permanently delete documents (requires two-factor verification)")
    op.bulk_insert(
        _role_permissions,
        [
            {"role_id": role, "permission_id": permission}
            for role, permission in [
                (_OWNER, _ARCHIVE_OWN),
                (_OWNER, _ARCHIVE_ANY),
                (_ADMIN, _ARCHIVE_OWN),
                (_ADMIN, _ARCHIVE_ANY),
                (_MEMBER, _ARCHIVE_OWN),
            ]
        ],
    )
    op.execute(
        sa.delete(_role_permissions).where(
            _role_permissions.c.role_id == _MEMBER, _role_permissions.c.permission_id == _DELETE
        )
    )


def _downgrade_permissions() -> None:
    op.bulk_insert(_role_permissions, [{"role_id": _MEMBER, "permission_id": _DELETE}])
    op.execute(sa.delete(_permissions).where(_permissions.c.id.in_([_ARCHIVE_OWN, _ARCHIVE_ANY])))
    _set_delete_description("Delete documents and collections")
