"""The Google Drive adapter's requests and responses, with HTTP mocked (ADR 0035)."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from documind.application.connectors.ports import (
    DOCX,
    GOOGLE_DOC,
    ConnectorAuthError,
    ConnectorError,
    DriveFile,
)
from documind.infrastructure.connectors.google_drive import GoogleDriveClient


def client(handler: object) -> tuple[GoogleDriveClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)  # type: ignore[operator]
        return response

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GoogleDriveClient(client_id="cid", client_secret="csecret", http=http), seen


def test_authorize_url_asks_for_offline_read_only_access_with_pkce() -> None:
    drive, _ = client(lambda r: httpx.Response(200))
    url = drive.authorize_url(state="s1", code_challenge="ch", redirect_uri="https://app/cb")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["s1"]


def test_code_exchange_and_refresh() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "oauth2.googleapis.com":
            form = parse_qs(r.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"] == ["v"]
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1"})
            if form["refresh_token"] == ["bad"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "a2"})
        assert r.headers["authorization"] == "Bearer a1"
        return httpx.Response(200, json={"user": {"emailAddress": "me@example.com"}})

    drive, seen = client(handler)
    tokens = asyncio.run(
        drive.exchange_code(code="c", code_verifier="v", redirect_uri="https://app/cb")
    )
    assert (tokens.refresh_token, tokens.account_email) == ("r1", "me@example.com")
    assert parse_qs(seen[0].content.decode())["client_secret"] == ["csecret"]
    assert asyncio.run(drive.access_token("r1")) == "a2"
    with pytest.raises(ConnectorAuthError):
        asyncio.run(drive.access_token("bad"))


def test_listing_changes_and_downloads() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith("/files") and "pageToken" not in r.url.params:
            assert "'fold\\'s' in parents" in r.url.params["q"]
            assert r.url.params["supportsAllDrives"] == "true"
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "1",
                            "name": "A.pdf",
                            "mimeType": "application/pdf",
                            "size": "5",
                            "parents": ["fold's"],
                        }
                    ],
                    "nextPageToken": "p2",
                },
            )
        if path.endswith("/files"):
            return httpx.Response(
                200, json={"files": [{"id": "2", "name": "B", "mimeType": GOOGLE_DOC}]}
            )
        if path.endswith("/changes"):
            return httpx.Response(
                200,
                json={
                    "newStartPageToken": "9",
                    "changes": [
                        {"fileId": "1", "removed": True},
                        {
                            "fileId": "2",
                            "removed": False,
                            "file": {
                                "id": "2",
                                "name": "B",
                                "mimeType": GOOGLE_DOC,
                                "trashed": True,
                            },
                        },
                    ],
                },
            )
        if path.endswith("/export"):
            assert r.url.params["mimeType"] == DOCX
            return httpx.Response(200, content=b"docx")
        if path.endswith("/files/1"):
            assert r.url.params["alt"] == "media"
            return httpx.Response(200, content=b"pdf")
        return httpx.Response(401)

    drive, _ = client(handler)
    files = asyncio.run(drive.children("t", "fold's"))
    assert [f.id for f in files] == ["1", "2"]  # both pages
    assert files[0].size == 5
    page = asyncio.run(drive.changes("t", "3"))
    assert page.new_start_page_token == "9"
    assert page.changes[0].removed
    assert page.changes[0].file is None
    assert page.changes[1].file is not None
    assert page.changes[1].file.trashed
    assert asyncio.run(drive.download("t", files[0])) == b"pdf"
    assert asyncio.run(drive.download("t", files[1])) == b"docx"
    with pytest.raises(ConnectorAuthError):
        asyncio.run(drive.download("t", DriveFile("x", "X.pdf", "application/pdf")))


def test_network_failures_are_temporary() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    drive, _ = client(handler)
    with pytest.raises(ConnectorError) as info:
        asyncio.run(drive.start_page_token("t"))
    assert not isinstance(info.value, ConnectorAuthError)
