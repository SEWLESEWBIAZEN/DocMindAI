"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from documind.infrastructure.database.models.billing import PlanRecord, UsageRecord, WorkspacePlan
from documind.infrastructure.database.models.conversations import (
    Conversation,
    ConversationCollection,
    ConversationDocument,
    Message,
    MessageCitation,
)
from documind.infrastructure.database.models.credentials import (
    Credential,
    WorkspaceAIProvider,
    WorkspaceAISettings,
)
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
from documind.infrastructure.database.models.infrastructure import (
    InfrastructureEvent,
    InfrastructureMigration,
    InfrastructureProfile,
    InfrastructureProfileVersion,
    WorkerRegistration,
    WorkspaceInfrastructure,
)
from documind.infrastructure.database.models.processing import (
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)

__all__ = [
    "AccountToken",
    "AuditLog",
    "Conversation",
    "ConversationCollection",
    "ConversationDocument",
    "Credential",
    "Document",
    "DocumentChunk",
    "DocumentCollection",
    "DocumentCollectionItem",
    "DocumentPage",
    "DocumentVersion",
    "InfrastructureEvent",
    "InfrastructureMigration",
    "InfrastructureProfile",
    "InfrastructureProfileVersion",
    "Invitation",
    "Message",
    "MessageCitation",
    "MfaRecoveryCode",
    "Organization",
    "Permission",
    "PlanRecord",
    "ProcessingJob",
    "RefreshToken",
    "Role",
    "UsageRecord",
    "User",
    "WorkerRegistration",
    "Workspace",
    "WorkspaceAIProvider",
    "WorkspaceAISettings",
    "WorkspaceInfrastructure",
    "WorkspaceMember",
    "WorkspacePlan",
    "role_permissions",
]
