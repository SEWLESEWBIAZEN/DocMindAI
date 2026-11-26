"""Slack Web API client (ADR 0037). Only fixed Slack hosts are ever called."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
API = "https://slack.com/api"
#: Bot scopes: slash commands, @mentions, and posting replies.
SCOPES = ("commands", "app_mentions:read", "chat:write")
RESPONSE_HOST = "hooks.slack.com"


class SlackError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SlackInstall:
    team_id: str
    team_name: str | None
    bot_user_id: str | None
    bot_token: str


class SlackClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 15.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._id,
            "scope": ",".join(SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def _call(
        self, method: str, *, token: str | None = None, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            if token is None:
                r = await self._http.post(
                    f"{API}/{method}", data=data or {}, auth=(self._id, self._secret)
                )
            else:
                r = await self._http.post(
                    f"{API}/{method}",
                    json=data or {},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise SlackError(f"{method}: Slack unreachable") from exc
        if r.status_code >= 400:
            raise SlackError(f"{method}: Slack returned {r.status_code}")
        body: dict[str, Any] = r.json()
        if not body.get("ok"):
            raise SlackError(f"{method}: {body.get('error', 'failed')}")
        return body

    async def exchange_code(self, *, code: str, redirect_uri: str) -> SlackInstall:
        body = await self._call(
            "oauth.v2.access", data={"code": code, "redirect_uri": redirect_uri}
        )
        team = body.get("team") or {}
        token = body.get("access_token")
        if body.get("token_type") != "bot" or not token or not team.get("id"):
            raise SlackError("oauth.v2.access: no bot token")
        return SlackInstall(
            team_id=str(team["id"]),
            team_name=team.get("name"),
            bot_user_id=body.get("bot_user_id"),
            bot_token=str(token),
        )

    async def post_message(
        self, token: str, *, channel: str, text: str, thread_ts: str | None = None
    ) -> None:
        data: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            data["thread_ts"] = thread_ts
        await self._call("chat.postMessage", token=token, data=data)

    async def respond(self, response_url: str, payload: dict[str, Any]) -> None:
        """Reply to a slash command through its response URL (only Slack's own host)."""
        if not is_response_url(response_url):
            raise SlackError("response_url is not a Slack URL")
        try:
            r = await self._http.post(response_url, json=payload)
        except httpx.HTTPError as exc:
            raise SlackError("response_url unreachable") from exc
        if r.status_code >= 400:
            raise SlackError(f"response_url returned {r.status_code}")

    async def revoke(self, token: str) -> None:
        await self._call("auth.revoke", token=token)


def is_response_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == RESPONSE_HOST and parts.port is None
