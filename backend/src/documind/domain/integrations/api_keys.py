"""Workspace API keys: scopes and what they may reach (ADR 0037).

A key acts as the person who created it, inside one workspace, and never with
more than its scopes allow. Admin work (members, billing, settings, keys,
SSO, infrastructure) is out of reach for every key, whoever created it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from types import MappingProxyType

from documind.domain.billing.plans import PlanKey
from documind.domain.identity.permissions import Permission

TOKEN_PREFIX = "dmk_"  # noqa: S105 - a prefix, not a secret
# Keys, the MCP server and Slack are included from Pro (and in the Pro trial).
INTEGRATION_PLANS = frozenset({PlanKey.PRO, PlanKey.BUSINESS, PlanKey.ENTERPRISE})
MAX_KEYS_PER_WORKSPACE = 25


class Scope(StrEnum):
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    ASK = "ask"


SCOPE_DESCRIPTIONS: MappingProxyType[Scope, str] = MappingProxyType(
    {
        Scope.DOCUMENTS_READ: "List, read and search documents and collections",
        Scope.DOCUMENTS_WRITE: "Upload documents and organize collections",
        Scope.ASK: "Ask questions and get cited answers",
    }
)

SCOPE_PERMISSIONS: MappingProxyType[Scope, frozenset[Permission]] = MappingProxyType(
    {
        Scope.DOCUMENTS_READ: frozenset({Permission.WORKSPACE_READ, Permission.DOCUMENTS_READ}),
        Scope.DOCUMENTS_WRITE: frozenset(
            {
                Permission.WORKSPACE_READ,
                Permission.DOCUMENTS_READ,
                Permission.DOCUMENTS_WRITE,
                Permission.DOCUMENTS_ARCHIVE_OWN,
            }
        ),
        Scope.ASK: frozenset(
            {Permission.WORKSPACE_READ, Permission.DOCUMENTS_READ, Permission.CONVERSATIONS_USE}
        ),
    }
)


def permissions_for(scopes: list[str] | tuple[str, ...]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for scope in scopes:
        try:
            granted |= SCOPE_PERMISSIONS[Scope(scope)]
        except ValueError:
            continue  # an unknown scope grants nothing
    return frozenset(granted)


# Route templates a key may call, relative to /api/v1/workspaces/{workspace_id}.
_KEY_ROUTES = re.compile(
    r"^(/(documents|collections|search|ask|conversations|extractions|deadlines|mcp)(/.*)?)?$"
)
_WORKSPACE_PREFIX = "/workspaces/{workspace_id}"


def route_allowed(template: str) -> bool:
    """Whether a key may call the route with this path template.

    The template may or may not carry the ``/api/v1`` mount prefix, depending on
    how the router was included.
    """
    template = template.removeprefix("/api/v1")
    if not template.startswith(_WORKSPACE_PREFIX):
        return False
    rest = template[len(_WORKSPACE_PREFIX) :]
    if rest.endswith("/access"):  # who can see a collection is admin work
        return False
    return bool(_KEY_ROUTES.match(rest))
