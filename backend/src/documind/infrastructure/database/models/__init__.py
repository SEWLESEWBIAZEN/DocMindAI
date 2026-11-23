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
    MfaRecoveryCode,
    Organization,
    Permission,
    RefreshToken,
    Role,
    User,
    Workspace,
    WorkspaceMember,
    role_permissions,
)
from documind.infrastructure.database.models.processing import (
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)

__all__ = [
    "AccountToken",
    "AuditLog",
    "Document",
    "DocumentChunk",
    "DocumentCollection",
    "DocumentCollectionItem",
    "DocumentPage",
    "DocumentVersion",
    "Invitation",
    "MfaRecoveryCode",
    "Organization",
    "Permission",
    "ProcessingJob",
    "RefreshToken",
    "Role",
    "User",
    "Workspace",
    "WorkspaceMember",
    "role_permissions",
]
