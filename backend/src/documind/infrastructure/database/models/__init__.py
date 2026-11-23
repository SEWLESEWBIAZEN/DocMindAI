"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from documind.infrastructure.database.models.identity import (
    AccountToken,
    AuditLog,
    Invitation,
    Organization,
    Permission,
    RefreshToken,
    Role,
    User,
    Workspace,
    WorkspaceMember,
    role_permissions,
)

__all__ = [
    "AccountToken",
    "AuditLog",
    "Invitation",
    "Organization",
    "Permission",
    "RefreshToken",
    "Role",
    "User",
    "Workspace",
    "WorkspaceMember",
    "role_permissions",
]
