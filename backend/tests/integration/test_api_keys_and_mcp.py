"""Workspace API keys and the MCP server (ADR 0037)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_collection_access_api import setup
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


def w(org: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{org}{tail}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_key(api: Api, org: str, elevated: dict[str, str], *scopes: str) -> str:
    r = api.client.post(
        w(org, "/api-keys"), json={"name": "Zapier", "scopes": list(scopes)}, headers=elevated
    )
    assert r.status_code == 201, r.text
    token = str(r.json()["token"])
    assert token.startswith("dmk_")
    return token


def rpc(api: Api, org: str, key: str, method: str, params: dict[str, Any] | None = None) -> Any:
    r = api.client.post(
        w(org, "/mcp"),
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
        headers=bearer(key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 7
    return body


def pro_org(api: Api) -> tuple[dict[str, Any], dict[str, str]]:
    s = setup(api)
    assign(api, s["org"], "pro")
    secret, _ = api.enable_mfa(s["owner"])
    return s, api.step_up(s["owner"], secret)


def test_keys_are_scoped_to_their_workspace_routes_and_scopes(api: Api) -> None:
    s, elevated = pro_org(api)
    org, owner = s["org"], s["owner"]

    # Creating needs an admin, a recent 2FA check and the Pro plan.
    body = {"name": "CI", "scopes": ["documents:read"]}
    assert api.client.post(w(org, "/api-keys"), json=body, headers=owner).json()["code"] == (
        "mfa_step_up_required"
    )
    assert api.client.post(w(org, "/api-keys"), json=body, headers=s["hr"]).status_code == 403
    personal = api.client.get("/api/v1/me/workspaces", headers=owner).json()
    mine = next(x["id"] for x in personal if x["kind"] == "personal")
    r = api.client.post(w(mine, "/api-keys"), json=body, headers=elevated)
    assert r.json()["code"] == "integrations_plan_required"

    reader = new_key(api, org, elevated, "documents:read")
    listed = api.client.get(w(org, "/api-keys"), headers=owner).json()
    assert [k["prefix"] for k in listed] == [reader[:12]]
    assert reader not in str(listed)
    stored = sql(api, "SELECT token_hash FROM api_keys")[0].token_hash
    assert reader not in stored
    assert len(stored) == 64

    # Reads work; the owner's key still doesn't see a restricted collection it isn't in.
    docs = api.client.get(w(org, "/documents"), headers=bearer(reader))
    assert docs.status_code == 200, docs.text
    assert {d["title"] for d in docs.json()["items"]} == {"handbook"}
    hits = api.client.post(
        w(org, "/search"), json={"query": "bicycles basement"}, headers=bearer(reader)
    )
    assert hits.status_code == 200
    assert hits.json()["results"]

    # Outside its scopes, its routes and its workspace: refused.
    asked = api.client.post(w(org, "/ask"), json={"question": "Where?"}, headers=bearer(reader))
    assert asked.status_code == 403
    upload = api.client.post(
        w(org, "/documents"),
        files={"file": ("a.md", b"# A\n\nText.", "text/markdown")},
        headers=bearer(reader),
    )
    assert upload.status_code == 403
    for path in ("/members", "/api-keys", "/sso", "/billing", "/integrations"):
        r = api.client.get(w(org, path), headers=bearer(reader))
        assert r.status_code == 403, path
        assert r.json()["code"] == "api_key_not_allowed"
    r = api.client.get(w(org, f"/collections/{s['col']}/access"), headers=bearer(reader))
    assert r.status_code == 403
    assert api.client.get("/api/v1/me", headers=bearer(reader)).json()["code"] == (
        "api_key_not_allowed"
    )
    assert api.client.get(w(mine, "/documents"), headers=bearer(reader)).status_code == 404
    assert api.client.get(w(org, "/documents"), headers=bearer("dmk_nope")).status_code == 401

    used = sql(api, "SELECT last_used_at FROM api_keys")[0].last_used_at
    assert used is not None
    actions = {r.action for r in sql(api, "SELECT action FROM audit_logs")}
    assert "api_key.created" in actions

    # Downgrading stops keys; so does revoking.
    assign(api, org, "free")
    assert api.client.get(w(org, "/documents"), headers=bearer(reader)).status_code == 403
    assign(api, org, "pro")
    key_id = listed[0]["id"]
    assert api.client.delete(w(org, f"/api-keys/{key_id}"), headers=owner).status_code == 204
    assert api.client.get(w(org, "/documents"), headers=bearer(reader)).status_code == 401
    assert api.client.get(w(org, "/api-keys"), headers=owner).json() == []


def test_mcp_server_tools(api: Api) -> None:
    s, elevated = pro_org(api)
    org = s["org"]
    key = new_key(api, org, elevated, "documents:read", "ask")

    init = rpc(api, org, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert init["result"]["serverInfo"]["name"] == "documind"
    newer = rpc(api, org, key, "initialize", {"protocolVersion": "2099-01-01"})
    assert newer["result"]["protocolVersion"] == "2025-11-25"

    # Notifications get 202 and no body.
    r = api.client.post(
        w(org, "/mcp"),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=bearer(key),
    )
    assert r.status_code == 202
    assert r.content == b""

    tools = {t["name"]: t for t in rpc(api, org, key, "tools/list")["result"]["tools"]}
    assert set(tools) == {"search", "ask", "list_documents", "read_document", "list_collections"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())

    found = rpc(api, org, key, "tools/call", {"name": "search", "arguments": {"query": "bicycles"}})
    result = found["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["results"][0]["document_title"] == "handbook"
    assert "basement" in result["content"][0]["text"]
    hidden = rpc(
        api,
        org,
        key,
        "tools/call",
        {"name": "search", "arguments": {"query": "engineering salary band level five"}},
    )
    titles = {h["document_title"] for h in hidden["result"]["structuredContent"]["results"]}
    assert "salaries" not in titles

    answer = rpc(
        api,
        org,
        key,
        "tools/call",
        {"name": "ask", "arguments": {"question": "Where do bicycles go?"}},
    )["result"]
    assert answer["structuredContent"]["sources"], answer
    assert "Sources:" in answer["content"][0]["text"]

    listed = rpc(api, org, key, "tools/call", {"name": "list_documents", "arguments": {}})
    docs = listed["result"]["structuredContent"]["documents"]
    assert [d["title"] for d in docs] == ["handbook"]
    page = rpc(
        api,
        org,
        key,
        "tools/call",
        {"name": "read_document", "arguments": {"document_id": docs[0]["id"]}},
    )["result"]
    assert "Bicycles go in the basement" in page["content"][0]["text"]
    secret_doc = rpc(
        api,
        org,
        key,
        "tools/call",
        {"name": "read_document", "arguments": {"document_id": s["secret"]}},
    )["result"]
    assert secret_doc["isError"] is True  # hidden: reported as not found
    bad = rpc(api, org, key, "tools/call", {"name": "search", "arguments": {"query": ""}})
    assert bad["result"]["isError"] is True
    assert rpc(api, org, key, "nope")["error"]["code"] == -32601
    assert rpc(api, org, key, "tools/call", {"name": "delete_all"})["error"]["code"] == -32602

    # A key without "ask" doesn't get that tool.
    reader = new_key(api, org, elevated, "documents:read")
    names = {t["name"] for t in rpc(api, org, reader, "tools/list")["result"]["tools"]}
    assert "ask" not in names
    denied = rpc(api, org, reader, "tools/call", {"name": "ask", "arguments": {"question": "x"}})
    assert denied["result"]["isError"] is True

    # Transport rules.
    r = api.client.post(
        w(org, "/mcp"),
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**bearer(key), "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403
    r = api.client.post(
        w(org, "/mcp"),
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**bearer(key), "MCP-Protocol-Version": "1999-01-01"},
    )
    assert r.status_code == 400
    assert api.client.get(w(org, "/mcp"), headers=bearer(key)).status_code == 405
    r = api.client.post(w(org, "/mcp"), content=b"{", headers=bearer(key))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700
    assert api.client.post(w(org, "/mcp"), json={}).status_code == 401
