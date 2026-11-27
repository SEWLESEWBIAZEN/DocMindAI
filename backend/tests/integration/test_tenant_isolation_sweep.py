"""Cross-tenant sweep over every workspace route (production readiness, Phase 4).

The routes are read from the running application's OpenAPI document, so a route added
later is covered without editing this file. Two attackers:

1. **Outsider:** a signed-in owner of another organization (with a fresh 2FA check, so
   step-up requirements don't hide anything) calls every workspace-scoped operation on
   the victim's workspace.
2. **ID substitution (IDOR):** the same attacker calls every operation that takes a
   resource ID on *their own* workspace, with the victim's real resource IDs.

Neither may ever get a 2xx, and the victim's data must be unchanged afterwards.
"""

# ruff: noqa: F811
from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_infrastructure import (  # noqa: F401
    POLICY,
    iapi,
    owner_org,
    ready,
    run_routed_job,
)

pytestmark = pytest.mark.integration

METHODS = ("get", "post", "put", "patch", "delete")
#: Answers that leak nothing: not found, forbidden, or the route doesn't apply.
SAFE = {403, 404}


def operations(api: Api) -> list[tuple[str, str]]:
    spec = api.client.app.openapi()  # type: ignore[attr-defined]
    return [
        (m, path)
        for path, item in spec["paths"].items()
        for m in item
        if m in METHODS and "{workspace_id}" in path
    ]


def victim(api: Api) -> dict[str, Any]:
    w, h, elevated = owner_org(api, "Victim Co")
    doc = ready(api, w, h)
    base = f"/api/v1/workspaces/{w}"
    conv = api.client.post(f"{base}/conversations", json={}, headers=h).json()["id"]
    sent = api.client.post(
        f"{base}/conversations/{conv}/messages", json={"content": "How much notice?"}, headers=h
    )
    assert sent.status_code in (200, 201), sent.text
    messages = api.client.get(f"{base}/conversations/{conv}", headers=h).json()["messages"]
    col = api.client.post(f"{base}/collections", json={"name": "Board"}, headers=h).json()["id"]
    inv = api.client.post(
        f"{base}/invitations", json={"email": "invitee@example.com"}, headers=elevated
    ).json()["id"]
    key = api.client.post(
        f"{base}/api-keys", json={"name": "CI", "scopes": ["documents:read"]}, headers=elevated
    ).json()["id"]
    members = api.client.get(f"{base}/members", headers=h).json()
    return {
        "workspace_id": w,
        "headers": h,
        "ids": {
            "document_id": doc,
            "conversation_id": conv,
            "message_id": messages[-1]["id"],
            "collection_id": col,
            "invitation_id": inv,
            "key_id": key,
            "member_id": members[0]["id"],
            "provider": "openai",
        },
    }


def fill(path: str, ids: dict[str, str], workspace_id: str) -> str:
    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "workspace_id":
            return workspace_id
        return ids.get(name, str(uuid.uuid4()))

    return re.sub(r"\{(\w+)\}", one, path)


def call(api: Api, method: str, path: str, headers: dict[str, str]) -> int:
    kwargs: dict[str, Any] = {"headers": headers}
    if method in ("post", "put", "patch"):
        kwargs["json"] = {}
    return api.client.request(method.upper(), path, **kwargs).status_code


def snapshot(api: Api, v: dict[str, Any]) -> dict[str, Any]:
    base, h = f"/api/v1/workspaces/{v['workspace_id']}", v["headers"]
    return {
        "documents": api.client.get(f"{base}/documents", headers=h).json()["total"],
        "conversation": len(
            api.client.get(f"{base}/conversations/{v['ids']['conversation_id']}", headers=h).json()[
                "messages"
            ]
        ),
        "collections": len(api.client.get(f"{base}/collections", headers=h).json()),
        "members": len(api.client.get(f"{base}/members", headers=h).json()),
        "keys": len(api.client.get(f"{base}/api-keys", headers=h).json()),
    }


def test_no_workspace_route_answers_an_outsider(iapi: Api) -> None:
    api = iapi
    v = victim(api)
    before = snapshot(api, v)
    _, _, attacker_elevated = owner_org(api, "Attacker Co")
    ops = operations(api)
    assert len(ops) > 100  # the sweep really covers the API

    leaks = []
    for method, path in ops:
        url = fill(path, v["ids"], v["workspace_id"])
        status = call(api, method, url, attacker_elevated)
        if status not in SAFE:
            leaks.append(f"{method.upper()} {path} -> {status}")
    assert leaks == []
    assert snapshot(api, v) == before


def test_victim_ids_are_not_reachable_through_the_attackers_workspace(iapi: Api) -> None:
    api = iapi
    v = victim(api)
    before = snapshot(api, v)
    attacker_ws, _, attacker_elevated = owner_org(api, "Attacker Co")
    with_ids = [
        (m, p)
        for m, p in operations(api)
        if set(re.findall(r"\{(\w+)\}", p)) & (set(v["ids"]) - {"provider"})
    ]
    assert len(with_ids) > 40

    leaks = []
    for method, path in with_ids:
        url = fill(path, v["ids"], attacker_ws)
        status = call(api, method, url, attacker_elevated)
        if 200 <= status < 300:
            leaks.append(f"{method.upper()} {path} -> {status}")
    assert leaks == []
    assert snapshot(api, v) == before


def test_a_job_message_naming_another_workspace_is_refused(iapi: Api) -> None:
    """Workers route a job by the workspace in its message; it must be the job's own."""
    api = iapi
    victim_ws, h, _ = owner_org(api, "Victim Co")
    other_ws, _, _ = owner_org(api, "Attacker Co")
    assert upload(api, victim_ws, h, POLICY, "policy.md").status_code == 201
    job = api.dispatcher.dispatched[-1]

    assert run_routed_job(api, job, other_ws).status == "skipped"
    doc = api.client.get(f"/api/v1/workspaces/{victim_ws}/documents", headers=h).json()["items"][0]
    assert doc["status"] != "ready"  # nothing was processed under the wrong routing
    # The genuine message still works afterwards: the job was not claimed or spent.
    assert run_routed_job(api, job, victim_ws).status == "succeeded"


def test_an_api_key_only_works_in_its_own_workspace(iapi: Api) -> None:
    api = iapi
    v = victim(api)
    before = snapshot(api, v)
    attacker_ws, _, attacker_elevated = owner_org(api, "Attacker Co")
    made = api.client.post(
        f"/api/v1/workspaces/{attacker_ws}/api-keys",
        json={"name": "all", "scopes": ["documents:read", "documents:write", "ask"]},
        headers=attacker_elevated,
    )
    assert made.status_code == 201, made.text
    key = {"Authorization": f"Bearer {made.json()['token']}"}
    # It works at home...
    assert api.client.get(f"/api/v1/workspaces/{attacker_ws}/documents", headers=key).is_success

    leaks = []
    for method, path in operations(api):
        status = call(api, method, fill(path, v["ids"], v["workspace_id"]), key)
        if 200 <= status < 300:
            leaks.append(f"{method.upper()} {path} -> {status}")
    mcp = api.client.post(
        f"/api/v1/workspaces/{v['workspace_id']}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "notice"}},
        },
        headers=key,
    )
    if mcp.is_success:
        leaks.append(f"POST /mcp -> {mcp.status_code}")
    assert leaks == []
    assert snapshot(api, v) == before
