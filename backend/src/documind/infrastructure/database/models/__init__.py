"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from documind.infrastructure.database.models.billing import (
    BillingEvent,
    BillingSubscription,
    PlanRecord,
    UsageRecord,
    WorkspacePlan,
)
from documind.infrastructure.database.models.connectors import (
    ConnectorConnection,
    ConnectorItem,
)
from documind.infrastructure.database.models.conversations import (
    Conversation,
    ConversationCollection,
    ConversationDocument,
    Message,
    MessageCitation,
    MessageFeedback,
)
from documind.infrastructure.database.models.credentials import (
    Credential,
    WorkspaceAIProvider,
    WorkspaceAISettings,
)
from documind.infrastructure.database.models.documents import (
    CollectionMember,
    Document,
    DocumentCollection,
    DocumentCollectionItem,
    DocumentVersion,
)
from documind.infrastructure.database.models.extraction import (
    Deadline,
    ExtractionResult,
    ExtractionRun,
)
from documind.infrastructure.database.models.identity import (
    AccountToken,
    AuditLog,
    DataExport,
    Invitation,
    MfaRecoveryCode,
    MfaResetRequest,
    Organization,
    Permission,
    RefreshToken,
    Role,
    User,
    WebAuthnCredential,
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
from documind.infrastructure.database.models.integrations import (
    ApiKey,
    SlackInstallation,
    SlackUserLink,
)
from documind.infrastructure.database.models.processing import (
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)
from documind.infrastructure.database.models.sso import SsoConnection

__all__ = [
    "AccountToken",
    "ApiKey",
    "AuditLog",
    "BillingEvent",
    "BillingSubscription",
    "CollectionMember",
    "ConnectorConnection",
    "ConnectorItem",
    "Conversation",
    "ConversationCollection",
    "ConversationDocument",
    "Credential",
    "DataExport",
    "Deadline",
    "Document",
    "DocumentChunk",
    "DocumentCollection",
    "DocumentCollectionItem",
    "DocumentPage",
    "DocumentVersion",
    "ExtractionResult",
    "ExtractionRun",
    "InfrastructureEvent",
    "InfrastructureMigration",
    "InfrastructureProfile",
    "InfrastructureProfileVersion",
    "Invitation",
    "Message",
    "MessageCitation",
    "MessageFeedback",
    "MfaRecoveryCode",
    "MfaResetRequest",
    "Organization",
    "Permission",
    "PlanRecord",
    "ProcessingJob",
    "RefreshToken",
    "Role",
    "SlackInstallation",
    "SlackUserLink",
    "SsoConnection",
    "UsageRecord",
    "User",
    "WebAuthnCredential",
    "WorkerRegistration",
    "Workspace",
    "WorkspaceAIProvider",
    "WorkspaceAISettings",
    "WorkspaceInfrastructure",
    "WorkspaceMember",
    "WorkspacePlan",
    "role_permissions",
]
