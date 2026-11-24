"""Permission catalog and role policy.

This module is the single source of truth for which permissions exist and
which roles grant them. The database ``roles``/``permissions`` tables are
seeded from it by migrations, and a test asserts the two never drift.
Authorization code checks *permissions*, never role names.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Permission(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_UPDATE = "workspace.update"
    MEMBERS_READ = "members.read"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_MANAGE = "members.manage"
    AUDIT_READ = "audit.read"
    USAGE_READ = "usage.read"
    DOCUMENTS_READ = "documents.read"
    DOCUMENTS_WRITE = "documents.write"
    DOCUMENTS_ARCHIVE_OWN = "documents.archive_own"
    DOCUMENTS_ARCHIVE_ANY = "documents.archive_any"
    DOCUMENTS_DELETE = "documents.delete"
    CONVERSATIONS_USE = "conversations.use"
    BILLING_MANAGE = "billing.manage"
    AI_PROVIDERS_MANAGE = "ai_providers.manage"
    INFRASTRUCTURE_MANAGE = "infrastructure.manage"


PERMISSION_DESCRIPTIONS: MappingProxyType[Permission, str] = MappingProxyType(
    {
        Permission.WORKSPACE_READ: "View the workspace and its settings",
        Permission.WORKSPACE_UPDATE: "Change workspace settings",
        Permission.MEMBERS_READ: "View workspace members",
        Permission.MEMBERS_INVITE: "Invite people and manage pending invitations",
        Permission.MEMBERS_MANAGE: "Change member roles and remove members",
        Permission.AUDIT_READ: "View the audit log",
        Permission.USAGE_READ: "View usage and quotas",
        Permission.DOCUMENTS_READ: "View and download documents",
        Permission.DOCUMENTS_WRITE: "Upload and edit documents and collections",
        Permission.DOCUMENTS_ARCHIVE_OWN: "Archive and restore documents you uploaded",
        Permission.DOCUMENTS_ARCHIVE_ANY: "Archive and restore any document",
        Permission.DOCUMENTS_DELETE: (
            "Permanently delete documents (requires two-factor verification)"
        ),
        Permission.CONVERSATIONS_USE: "Ask questions and hold conversations",
        Permission.BILLING_MANAGE: "Manage plan and billing",
        Permission.AI_PROVIDERS_MANAGE: (
            "Manage the workspace's own AI provider keys (requires two-factor verification)"
        ),
        Permission.INFRASTRUCTURE_MANAGE: (
            "Manage the organization's databases, storage and processing "
            "(requires two-factor verification)"
        ),
    }
)


class RoleKey(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


# Higher rank = more authority. Used by the role-assignment policy.
ROLE_RANK: MappingProxyType[RoleKey, int] = MappingProxyType(
    {RoleKey.OWNER: 40, RoleKey.ADMIN: 30, RoleKey.MEMBER: 20, RoleKey.VIEWER: 10}
)

ROLE_DESCRIPTIONS: MappingProxyType[RoleKey, str] = MappingProxyType(
    {
        RoleKey.OWNER: "Full control, including billing and ownership",
        RoleKey.ADMIN: "Manages members, settings and all content",
        RoleKey.MEMBER: "Works with documents and conversations",
        RoleKey.VIEWER: "Reads documents and asks questions",
    }
)

_VIEWER = frozenset(
    {
        Permission.WORKSPACE_READ,
        Permission.MEMBERS_READ,
        Permission.DOCUMENTS_READ,
        Permission.CONVERSATIONS_USE,
    }
)
_MEMBER = _VIEWER | {Permission.DOCUMENTS_WRITE, Permission.DOCUMENTS_ARCHIVE_OWN}
_ADMIN = _MEMBER | {
    Permission.WORKSPACE_UPDATE,
    Permission.MEMBERS_INVITE,
    Permission.MEMBERS_MANAGE,
    Permission.AUDIT_READ,
    Permission.USAGE_READ,
    Permission.DOCUMENTS_ARCHIVE_ANY,
    Permission.DOCUMENTS_DELETE,
    Permission.AI_PROVIDERS_MANAGE,
}
_OWNER = frozenset(Permission)

ROLE_PERMISSIONS: MappingProxyType[RoleKey, frozenset[Permission]] = MappingProxyType(
    {
        RoleKey.OWNER: _OWNER,
        RoleKey.ADMIN: frozenset(_ADMIN),
        RoleKey.MEMBER: frozenset(_MEMBER),
        RoleKey.VIEWER: _VIEWER,
    }
)
