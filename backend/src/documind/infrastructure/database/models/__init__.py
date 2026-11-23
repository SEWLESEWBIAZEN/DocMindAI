"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from documind.infrastructure.database.models.documents import (
    Document,
    DocumentCollection,
    DocumentCollectionItem,
    DocumentVersion,
)
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
    "Document",
    "DocumentCollection",
    "DocumentCollectionItem",
    "DocumentVersion",
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
