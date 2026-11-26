"""The Slack app (ADR 0037), against a fake Slack Web API.

Requests from "Slack" are signed with the signing secret, exactly as Slack
signs them, so the verification code runs for real.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from pydantic import SecretStr

from documind.container import build_container
from documind.infrastructure.integrations.slack import SlackClient
from documind.worker.runtime import slack_answer
from tests.integration.conftest import Api
from tests.integration.test_collection_access_api import setup
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration

SIGNING = "8f742231b10e8888abcd99yyyzzz85a5"
TEAM = "T0ACME"
RESPONSE_URL = "https://hooks.slack.com/commands/T0ACME/1/abc"
BOT_TOKEN = "xoxb-test-bot-token"


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.revoked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://slack.com/api/oauth.v2.access":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["good-code"]
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token_type": "bot",
                    "access_token": BOT_TOKEN,
                    "bot_user_id": "UBOT",
                    "team": {"id": TEAM, "name": "Acme Slack"},
                },
            )
        if url == "https://slack.com/api/chat.postMessage":
            assert request.headers["authorization"] == f"Bearer {BOT_TOKEN}"
            self.posts.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if url == "https://slack.com/api/auth.revoke":
            self.revoked.append(request.headers["authorization"])
            return httpx.Response(200, json={"ok": True, "revoked": True})
        if url == RESPONSE_URL:
            self.responses.append(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(404, json={"ok": False, "error": "unknown"})

    def client(self) -> SlackClient:
        return SlackClient(
            client_id="111.222",
            client_secret="client-secret",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def jobs() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def sapi(make_api: Any, slack: FakeSlack, jobs: list[dict[str, Any]]) -> Api:
    async def dispatch(job: dict[str, Any]) -> bool:
        jobs.append(job)
        return True

    api: Api = make_api(
        {"slack_client": slack.client(), "slack_dispatch": dispatch},
        slack_client_id="111.222",
        slack_client_secret=SecretStr("client-secret"),
        slack_signing_secret=SecretStr(SIGNING),
    )
    return api


def signed(body: bytes, *, ts: int | None = None, secret: str = SIGNING) -> dict[str, str]:
    stamp = str(ts if ts is not None else int(time.time()))
    sig = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256)
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": "v0=" + sig.hexdigest()}


def command(api: Api, user: str, text: str) -> Any:
    body = urlencode(
        {
            "team_id": TEAM,
            "user_id": user,
            "command": "/documind",
            "text": text,
            "response_url": RESPONSE_URL,
        }
    ).encode()
    r = api.client.post(
        "/api/v1/integrations/slack/commands",
        content=body,
        headers={**signed(body), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def event(api: Api, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    return api.client.post(
        "/api/v1/integrations/slack/events",
        content=body,
        headers={**signed(body), "Content-Type": "application/json"},
    )


def run_jobs(api: Api, slack: FakeSlack, jobs: list[dict[str, Any]]) -> list[str]:
    async def go() -> list[str]:
        c = build_container(api.settings)
        c.slack_client = slack.client()
        try:
            return [await slack_answer(job, c) for job in jobs]
        finally:
            await c.aclose()

    out = asyncio.run(go())
    jobs.clear()
    return out


def install(api: Api, org: str, elevated: dict[str, str]) -> None:
    r = api.client.post(f"/api/v1/workspaces/{org}/integrations/slack/authorize", headers=elevated)
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "slack.com"
    assert q["scope"] == ["commands,app_mentions:read,chat:write"]
    back = api.client.get(
        "/api/v1/integrations/slack/callback",
        params={"state": q["state"][0], "code": "good-code"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    assert back.headers["location"].endswith(f"/w/{org}/integrations?slack=connected")


def test_install_link_and_answer_privately_and_in_channels(
    sapi: Api, slack: FakeSlack, jobs: list[dict[str, Any]]
) -> None:
    s = setup(sapi)
    org, owner, hr = s["org"], s["owner"], s["hr"]
    assign(sapi, org, "pro")
    secret, _ = sapi.enable_mfa(owner)
    elevated = sapi.step_up(owner, secret)

    # Only admins, with 2FA, can add the app; the bot token goes to the vault.
    denied = sapi.client.post(f"/api/v1/workspaces/{org}/integrations/slack/authorize", headers=hr)
    assert denied.status_code == 403
    install(sapi, org, elevated)
    stored = sql(sapi, "SELECT ciphertext FROM credentials WHERE kind = 'slack_bot_token'")
    assert stored
    assert BOT_TOKEN not in stored[0].ciphertext
    overview = sapi.client.get(f"/api/v1/workspaces/{org}/integrations", headers=hr).json()
    assert overview["slack"]["connected"]
    assert overview["slack"]["team_name"] == "Acme Slack"
    assert overview["slack"]["linked"] is False

    # Signatures are checked before anything else.
    body = b"team_id=T0ACME&user_id=U1&text=help"
    for headers in (
        {},
        signed(body, secret="wrong"),
        signed(body, ts=int(time.time()) - 600),
    ):
        r = sapi.client.post("/api/v1/integrations/slack/commands", content=body, headers=headers)
        assert r.status_code == 401

    assert "answers questions" in command(sapi, "U_HR", "help")["text"]
    # Not linked yet: no answer, just how to link.
    assert "Link your DocuMind account" in command(sapi, "U_HR", "what is the salary band?")["text"]
    assert jobs == []

    # Linking: a code made in DocuMind, typed into Slack. Single use.
    code = sapi.client.post(
        f"/api/v1/workspaces/{org}/integrations/slack/link-code", headers=hr
    ).json()["code"]
    assert command(sapi, "U_HR", f"link {code}")["text"].startswith("Linked")
    assert "expired" in command(sapi, "U_STAFF", f"link {code}")["text"]
    assert "isn't a link code" in command(sapi, "U_STAFF", "link nope")["text"]
    me = sapi.client.get(f"/api/v1/workspaces/{org}/integrations", headers=hr).json()
    assert me["slack"]["linked"] is True

    # A slash command answers privately, with everything the asker may see.
    reply = command(sapi, "U_HR", "What is the engineering salary band for level five?")
    assert reply["response_type"] == "ephemeral"
    assert [j["kind"] for j in jobs] == ["command"]
    assert run_jobs(sapi, slack, jobs) == ["answered"]
    private = slack.responses[-1]
    assert private["response_type"] == "ephemeral"
    assert "salaries" in private["text"], private
    assert f"/w/{org}/documents/{s['secret']}" in private["text"]

    # A mention answers in the thread, from documents everyone may see.
    mention = {
        "type": "event_callback",
        "team_id": TEAM,
        "event_id": "Ev1",
        "event": {
            "type": "app_mention",
            "user": "U_HR",
            "text": "<@UBOT> What is the engineering salary band for level five?",
            "channel": "C1",
            "ts": "1700000000.000100",
        },
    }
    assert event(sapi, mention).status_code == 200
    assert event(sapi, mention).status_code == 200  # Slack retried: ignored
    assert len(jobs) == 1
    assert jobs[0]["question"].startswith("What is")
    assert run_jobs(sapi, slack, jobs) == ["answered"]
    public = slack.posts[-1]
    assert public["channel"] == "C1"
    assert public["thread_ts"] == "1700000000.000100"
    assert "salaries" not in public["text"]
    assert "everyone in the workspace can see" in public["text"]

    # Someone unlinked who mentions the bot is told how to link, in the thread.
    other = {**mention, "event_id": "Ev2", "event": {**mention["event"], "user": "U_STAFF"}}
    event(sapi, other)
    assert run_jobs(sapi, slack, jobs) == ["not_linked"]
    assert "Link your DocuMind account" in slack.posts[-1]["text"]

    challenge = event(sapi, {"type": "url_verification", "challenge": "abc123"})
    assert challenge.json() == {"challenge": "abc123"}

    actions = {r.action for r in sql(sapi, "SELECT action FROM audit_logs")}
    assert {"slack.installed", "slack.user_linked"} <= actions

    # Removing the app revokes the token at Slack and forgets the links.
    gone = sapi.client.delete(f"/api/v1/workspaces/{org}/integrations/slack", headers=elevated)
    assert gone.status_code == 204
    assert slack.revoked == [f"Bearer {BOT_TOKEN}"]
    assert sql(sapi, "SELECT count(*) AS n FROM slack_user_links")[0].n == 0
    assert "isn't connected" in command(sapi, "U_HR", "anything")["text"]


def test_callback_is_bound_to_the_browser_and_one_team_per_workspace(
    sapi: Api, slack: FakeSlack
) -> None:
    s = setup(sapi)
    org, owner = s["org"], s["owner"]
    assign(sapi, org, "pro")
    secret, _ = sapi.enable_mfa(owner)
    elevated = sapi.step_up(owner, secret)
    r = sapi.client.post(f"/api/v1/workspaces/{org}/integrations/slack/authorize", headers=elevated)
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    sapi.client.cookies.clear()
    back = sapi.client.get(
        "/api/v1/integrations/slack/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert back.headers["location"].endswith("slack_error=browser")
    assert sql(sapi, "SELECT count(*) AS n FROM slack_installations")[0].n == 0

    # The same Slack team can't be connected to a second workspace.
    install(sapi, org, elevated)
    other = str(sapi.create_org(owner, "Beta")["id"])
    assign(sapi, other, "pro")
    r = sapi.client.post(
        f"/api/v1/workspaces/{other}/integrations/slack/authorize", headers=elevated
    )
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    back = sapi.client.get(
        "/api/v1/integrations/slack/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert back.headers["location"].endswith("slack_error=team_taken")

    # Free plans don't include Slack.
    assign(sapi, other, "free")
    r = sapi.client.post(
        f"/api/v1/workspaces/{other}/integrations/slack/authorize", headers=elevated
    )
    assert r.json()["code"] == "integrations_plan_required"
    del slack
