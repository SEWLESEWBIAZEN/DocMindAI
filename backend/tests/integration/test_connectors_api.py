"""Google Drive connector against real services, with Drive itself faked (ADR 0035)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from documind.application.connectors.ports import (
    DOCX,
    FOLDER,
    GOOGLE_DOC,
    Change,
    ChangePage,
    ConnectorAuthError,
    DriveFile,
    Tokens,
)
from documind.application.connectors.sync import DataAccess, DriveSync, SyncReport
from documind.application.documents.document_service import DocumentService
from documind.application.identity.common import RequestMeta
from documind.container import build_container
from tests.integration.conftest import Api
from tests.integration.test_processing_pipeline import make_docx, run_job, sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


class FakeDrive:
    """Files, folders and a change feed in memory."""

    def __init__(self) -> None:
        self.files: dict[str, DriveFile] = {}
        self.content: dict[str, bytes] = {}
        self.feed: list[Change] = []
        self.revoked = False
        self.revoked_tokens: list[str] = []

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}&challenge={code_challenge}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> Tokens:
        assert code == "good-code"
        assert redirect_uri.endswith("/api/v1/connectors/google-drive/callback")
        return Tokens("refresh-123", "access", "drive.owner@example.com")

    async def access_token(self, refresh_token: str) -> str:
        if self.revoked:
            raise ConnectorAuthError("invalid_grant")
        assert refresh_token == "refresh-123"
        return "access"

    async def revoke(self, token: str) -> None:
        self.revoked_tokens.append(token)

    async def folders(self, token: str, parent: str | None) -> list[DriveFile]:
        if parent is None:
            return [DriveFile("root", "My Drive", FOLDER)]
        return [f for f in self.files.values() if f.is_folder and parent in f.parents]

    async def children(self, token: str, folder_id: str) -> list[DriveFile]:
        return [f for f in self.files.values() if folder_id in f.parents and not f.trashed]

    async def start_page_token(self, token: str) -> str:
        return str(len(self.feed))

    async def changes(self, token: str, page_token: str) -> ChangePage:
        start = int(page_token)
        return ChangePage(self.feed[start:], None, str(len(self.feed)))

    async def download(self, token: str, file: DriveFile) -> bytes:
        return self.content[file.id]

    # --- test helpers -------------------------------------------------------

    def put(self, fid: str, name: str, mime: str, parent: str, body: bytes = b"") -> DriveFile:
        file = DriveFile(
            fid,
            name,
            mime,
            (parent,),
            hashlib.md5(body, usedforsecurity=False).hexdigest() if body else None,
            "t1",
        )
        self.files[fid] = file
        self.content[fid] = body
        self.feed.append(Change(fid, False, file))
        return file

    def change(self, fid: str, **kw: Any) -> None:
        file = replace(self.files[fid], **kw)
        if "body" in kw:
            raise TypeError
        self.files[fid] = file
        self.feed.append(Change(fid, False, file))

    def edit(self, fid: str, body: bytes) -> None:
        self.content[fid] = body
        self.change(fid, md5=hashlib.md5(body, usedforsecurity=False).hexdigest())


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, connection_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        self.calls.append((connection_id, workspace_id))
        return True


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def capi(make_api: Any, drive: FakeDrive, recorder: Recorder) -> Api:
    api: Api = make_api({"drive_client": drive, "connector_dispatch": recorder})
    return api


def sync(api: Api, drive: FakeDrive, connection_id: str) -> SyncReport:
    async def go() -> SyncReport:
        c = build_container(api.settings)
        try:

            async def data_for(workspace_id: uuid.UUID) -> DataAccess:
                return DataAccess(
                    c.session_factory,
                    lambda s: DocumentService(
                        s, api.settings, c.storage, c.malware_scanner, RequestMeta(), api.dispatcher
                    ),
                )

            return await DriveSync(
                c.session_factory,
                drive,
                c.cipher,
                data_for,
                max_files=100,
                max_bytes=api.settings.max_upload_bytes,
            ).run(uuid.UUID(connection_id))
        finally:
            await c.aclose()

    return asyncio.run(go())


def url(w: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{w}/connectors{tail}"


def connect(api: Api) -> tuple[str, dict[str, str], dict[str, str], str]:
    """An organization whose admin connected Drive. Returns ids, headers, connection."""
    h = api.signup("admin@example.com")
    org = str(api.create_org(h, "Acme")["id"])
    secret, _ = api.enable_mfa(h)
    elevated = api.step_up(h, secret)
    r = api.client.post(url(org, "/google-drive/authorize"), headers=elevated)
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    back = api.client.get(
        "/api/v1/connectors/google-drive/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    location = back.headers["location"]
    assert location.startswith(f"https://app.test.example/w/{org}/connectors?connected=")
    return org, h, elevated, location.rsplit("=", 1)[1]


def docs(api: Api, w: str, h: dict[str, str], archived: str = "exclude") -> dict[str, Any]:
    items = api.client.get(
        f"/api/v1/workspaces/{w}/documents", params={"archived": archived}, headers=h
    ).json()["items"]
    return {d["title"]: d for d in items}


def test_connect_choose_folders_and_sync_changes(
    capi: Api, drive: FakeDrive, recorder: Recorder
) -> None:
    drive.put("fold", "Contracts", FOLDER, "root")
    drive.put("sub", "2026", FOLDER, "fold")
    drive.put("a", "Policy.md", "text/markdown", "fold", b"# Policy\n\nRenew within 30 days.\n")
    drive.put("b", "Plan", GOOGLE_DOC, "sub", make_docx())
    drive.put("bad", "Broken.pdf", "application/pdf", "fold", b"not a pdf at all")
    drive.put("c", "photo.png", "image/png", "fold", b"\x89PNG")
    drive.put("elsewhere", "Other.md", "text/markdown", "root", b"# Other\n\nNot synced.\n")
    org, h, _elevated, cid = connect(capi)

    listing = capi.client.get(url(org), headers=h).json()
    assert listing["providers"][0]["available"] is True
    (conn,) = listing["connections"]
    assert conn["status"] == "needs_folders"
    assert conn["account_email"] == "drive.owner@example.com"
    # The refresh token is in the vault, encrypted, never in the API.
    assert "refresh-123" not in str(listing)
    stored = sql(capi, "SELECT ciphertext FROM credentials WHERE kind = 'connector_token'")
    assert stored
    assert "refresh-123" not in stored[0].ciphertext

    roots = capi.client.get(url(org, f"/{cid}/folders"), headers=h).json()
    assert roots == [{"id": "root", "name": "My Drive"}]
    inside = capi.client.get(url(org, f"/{cid}/folders"), params={"parent": "root"}, headers=h)
    assert inside.json() == [{"id": "fold", "name": "Contracts"}]

    configured = capi.client.put(
        url(org, f"/{cid}"), json={"folders": [{"id": "fold", "name": "Contracts"}]}, headers=h
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["status"] == "active"
    assert recorder.calls[-1] == (uuid.UUID(cid), uuid.UUID(org))

    report = sync(capi, drive, cid)
    # The PNG (unsupported) and the broken PDF are skipped; the sync carries on.
    assert (report.imported, report.skipped, report.status) == (2, 2, "ok")
    found = docs(capi, org, h)
    assert set(found) == {"Policy", "Plan"}  # the PNG and the file outside are left out
    assert found["Plan"]["original_filename"] == "Plan.docx"
    collection = configured.json()["collection_id"]
    assert collection in found["Policy"]["collection_ids"]
    state = capi.client.get(url(org), headers=h).json()["connections"][0]
    assert state["initial_sync_done"] is True
    assert state["files_synced"] == 2

    # Nothing changed: nothing happens.
    assert sync(capi, drive, cid).imported == 0
    # Edited in Drive: a new version.
    drive.edit("a", b"# Policy\n\nRenew within 60 days.\n")
    report = sync(capi, drive, cid)
    assert report.updated == 1
    assert docs(capi, org, h)["Policy"]["version_count"] == 2
    # New file in a new subfolder.
    drive.put("sub2", "Q3", FOLDER, "fold")
    drive.put("d", "Q3 notes.md", "text/markdown", "sub2", b"# Q3\n\nNotes.\n")
    assert sync(capi, drive, cid).imported == 1
    # Trashed, then moved out: archived, never deleted.
    drive.change("d", trashed=True)
    drive.change("a", parents=("root",))
    report = sync(capi, drive, cid)
    assert report.archived == 2
    assert set(docs(capi, org, h)) == {"Plan"}
    assert {"Q3 notes", "Policy"} <= set(docs(capi, org, h, "only"))
    # Moved back in: restored.
    drive.change("a", parents=("fold",))
    sync(capi, drive, cid)
    assert "Policy" in docs(capi, org, h)
    # Imports are processed like uploads.
    for job in capi.dispatcher.dispatched:
        run_job(capi, job)
    assert docs(capi, org, h)["Policy"]["status"] == "ready"


def test_browser_binding_revocation_quota_and_disconnect(
    capi: Api, drive: FakeDrive, recorder: Recorder
) -> None:
    drive.put("fold", "Docs", FOLDER, "root")
    for i in range(3):
        drive.put(
            f"f{i}", f"Note {i}.md", "text/markdown", "fold", f"# Note {i}\n\nText.\n".encode()
        )

    # Connecting needs 2FA.
    h0 = capi.signup("nomfa@example.com")
    org0 = str(capi.create_org(h0, "Beta")["id"])
    assert (
        capi.client.post(url(org0, "/google-drive/authorize"), headers=h0).json()["code"]
        == "mfa_enrollment_required"
    )

    # A link started in another browser can't be completed here (no nonce cookie).
    org, h, elevated, _ = connect(capi)
    r = capi.client.post(url(org, "/google-drive/authorize"), headers=elevated)
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    capi.client.cookies.clear()
    victim = capi.client.get(
        "/api/v1/connectors/google-drive/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert victim.headers["location"].endswith("connector_error=browser")
    # And a used or unknown state is refused.
    again = capi.client.get(
        "/api/v1/connectors/google-drive/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert again.headers["location"].endswith("connector_error=expired")
    assert len(capi.client.get(url(org), headers=h).json()["connections"]) == 1

    # Members can't manage connectors.
    member = capi.add_member(org, h, "mo@example.com", "member")
    assert capi.client.post(url(org, "/google-drive/authorize"), headers=member).status_code == 403

    cid = capi.client.get(url(org), headers=h).json()["connections"][0]["id"]
    capi.client.put(
        url(org, f"/{cid}"), json={"folders": [{"id": "fold", "name": "Docs"}]}, headers=h
    )
    # Quota: the sync pauses and picks up later.
    assign(capi, org, "free", max_documents=2)
    report = sync(capi, drive, cid)
    assert report.status == "quota"
    state_now = capi.client.get(url(org), headers=h).json()["connections"][0]
    assert (state_now["status"], state_now["last_error"]) == ("paused", "quota_exceeded")
    assert state_now["initial_sync_done"] is False
    assign(capi, org, "free")
    report = sync(capi, drive, cid)
    assert report.imported == 1
    assert len(docs(capi, org, h)) == 3

    # Access revoked at Google: the connection asks to reconnect.
    drive.revoked = True
    assert sync(capi, drive, cid).status == "reconnect"
    state_now = capi.client.get(url(org), headers=h).json()["connections"][0]
    assert (state_now["status"], state_now["last_error"]) == ("error", "reconnect")
    assert (
        capi.client.post(url(org, f"/{cid}/sync"), headers=h).json()["code"]
        == "connector_reconnect"
    )

    # Disconnecting needs 2FA; documents stay, the token is destroyed.
    drive.revoked = False
    assert capi.client.delete(url(org, f"/{cid}"), headers=h).status_code == 403
    assert capi.client.delete(url(org, f"/{cid}"), headers=elevated).status_code == 204
    assert drive.revoked_tokens == ["refresh-123"]
    assert capi.client.get(url(org), headers=h).json()["connections"] == []
    assert len(docs(capi, org, h)) == 3
    assert sql(capi, "SELECT count(*) AS n FROM connector_items")[0].n == 0
    revoked = sql(capi, "SELECT status, ciphertext FROM credentials WHERE kind = 'connector_token'")
    assert all(r.status == "revoked" and r.ciphertext == "" for r in revoked)
    actions = {r.action for r in sql(capi, "SELECT action FROM audit_logs")}
    assert {"connector.connected", "connector.configured", "connector.disconnected"} <= actions


def test_not_configured(make_api: Any) -> None:
    api: Api = make_api()
    h = api.signup("x@example.com")
    org = str(api.create_org(h, "Gamma")["id"])
    listing = api.client.get(url(org), headers=h).json()
    assert listing["providers"][0]["available"] is False
    secret, _ = api.enable_mfa(h)
    elevated = api.step_up(h, secret)
    r = api.client.post(url(org, "/google-drive/authorize"), headers=elevated)
    assert r.json()["code"] == "connector_not_configured"
    assert DOCX.endswith("document")
