"""API key rules, Slack signatures and formatting, MCP protocol handling (ADR 0037)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from documind.application.integrations.mcp import McpServer
from documind.application.integrations.slack import (
    format_answer,
    new_link_code,
    verify_signature,
)
from documind.domain.errors import AuthenticationError
from documind.domain.identity.permissions import Permission
from documind.domain.integrations.api_keys import permissions_for, route_allowed
from documind.infrastructure.integrations.slack import is_response_url


@pytest.mark.parametrize(
    ("template", "allowed"),
    [
        ("/api/v1/workspaces/{workspace_id}/documents", True),
        ("/workspaces/{workspace_id}/documents/{document_id}/pages", True),
        ("/api/v1/workspaces/{workspace_id}/search", True),
        ("/api/v1/workspaces/{workspace_id}/mcp", True),
        ("/api/v1/workspaces/{workspace_id}", True),
        ("/api/v1/workspaces/{workspace_id}/collections/{collection_id}/access", False),
        ("/api/v1/workspaces/{workspace_id}/members", False),
        ("/api/v1/workspaces/{workspace_id}/api-keys", False),
        ("/api/v1/workspaces/{workspace_id}/documentsx", False),
        ("/api/v1/workspaces/{workspace_id}/integrations/slack", False),
        ("/api/v1/me", False),
        ("", False),
    ],
)
def test_routes_keys_may_call(template: str, allowed: bool) -> None:
    assert route_allowed(template) is allowed


def test_scopes_never_grant_admin_permissions() -> None:
    every = permissions_for(["documents:read", "documents:write", "ask", "admin"])
    assert Permission.WORKSPACE_UPDATE not in every
    assert Permission.MEMBERS_MANAGE not in every
    assert Permission.DOCUMENTS_DELETE not in every
    assert permissions_for(["ask"]) >= {Permission.CONVERSATIONS_USE, Permission.DOCUMENTS_READ}
    assert Permission.DOCUMENTS_WRITE not in permissions_for(["documents:read"])
    assert permissions_for(["nonsense"]) == frozenset()


def _sign(secret: str, ts: str, body: bytes) -> str:
    return (
        "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    )


def test_slack_signature() -> None:
    body, now = b"token=x&text=hi", 1_700_000_000.0
    good = _sign("s3", "1700000000", body)
    verify_signature("s3", timestamp="1700000000", body=body, signature=good, now=now)
    bad = [
        ("1700000000", body + b"x", good, now),  # body changed
        ("1700000000", body, _sign("other", "1700000000", body), now),
        ("1700000000", body, good, now + 301),  # replayed later
        ("abc", body, good, now),
        (None, body, good, now),
        ("1700000000", body, None, now),
    ]
    for ts, b, sig, at in bad:
        with pytest.raises(AuthenticationError):
            verify_signature("s3", timestamp=ts, body=b, signature=sig, now=at)


def test_link_codes_and_response_urls() -> None:
    codes = {new_link_code() for _ in range(50)}
    assert len(codes) == 50
    assert all(len(c) == 9 and c[4] == "-" and not set(c) & set("01IO") for c in codes)
    assert is_response_url("https://hooks.slack.com/commands/T/1/x")
    assert not is_response_url("http://hooks.slack.com/commands/T/1/x")
    assert not is_response_url("https://hooks.slack.com.evil.example/x")
    assert not is_response_url("https://hooks.slack.com:8443/x")
    assert not is_response_url("https://169.254.169.254/latest")


def test_slack_answers_escape_and_cite() -> None:
    doc = uuid.uuid4()
    answer = SimpleNamespace(
        answer="Use <script> & <@U1> [1]",
        citations=[
            SimpleNamespace(number=1, document_id=doc, document_name="A|B <c>", page_start=3)
        ],
    )
    ws = uuid.uuid4()
    text = format_answer(answer, app_url="https://app.example/", workspace_id=ws, public=True)
    assert "&lt;script&gt; &amp; &lt;@U1&gt;" in text  # no mentions or links smuggled in
    assert f"<https://app.example/w/{ws}/documents/{doc}|A/B &lt;c&gt;>, p. 3" in text
    assert "everyone in the workspace" in text
    private = format_answer(answer, app_url="https://app.example", workspace_id=ws, public=False)
    assert "everyone" not in private


class _NoTools:
    ctx: Any = None

    def available(self) -> list[str]:
        return ["search"]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return f"called {name}", {"args": args}


def test_mcp_protocol() -> None:
    server = McpServer(_NoTools())  # type: ignore[arg-type]

    def handle(message: Any) -> Any:
        return asyncio.run(server.handle(message))

    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None  # a response to us
    assert handle({"id": 1, "method": "ping"})["error"]["code"] == -32600
    assert handle([1])["error"]["code"] == -32600
    assert handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"] == {}
    init = handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert init["result"]["protocolVersion"] == "2025-03-26"
    listed = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["search"]
    called = handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "x"}},
        }
    )
    assert called["result"]["content"][0]["text"] == "called search"
    not_allowed = handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ask"}}
    )
    assert not_allowed["result"]["isError"] is True
    bad_args = handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "search", "arguments": [1]},
        }
    )
    assert bad_args["error"]["code"] == -32602
    assert (
        handle({"jsonrpc": "2.0", "id": 7, "method": "x", "params": 3})["error"]["code"] == -32602
    )
