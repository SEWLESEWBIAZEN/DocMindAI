"""Account self-service (ADR 0039): email change, data export, account and organization deletion."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from typing import Any

import pytest

from documind.application.ports import ObjectNotFoundError
from documind.container import build_container
from documind.worker.runtime import data_export, sweep_data_exports
from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_conversations_api import base, create, send
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_rag_api import POLICY, SpyLLM, ingest

pytestmark = pytest.mark.integration


@pytest.fixture
def exports() -> list[uuid.UUID]:
    return []


@pytest.fixture
def sapi(make_api: Any, exports: list[uuid.UUID]) -> Api:
    async def dispatch(export_id: uuid.UUID) -> bool:
        exports.append(export_id)
        return True

    api: Api = make_api({"llm_provider": SpyLLM(), "export_dispatch": dispatch})
    return api


def run_async(api: Api, fn: Any) -> Any:
    async def go() -> Any:
        c = build_container(api.settings)
        c.email_sender = api.mail
        try:
            return await fn(c)
        finally:
            await c.aclose()

    return asyncio.run(go())


def stored(api: Api, key: str) -> bool:
    async def check(c: Any) -> bool:
        try:
            await c.storage.get_metadata(key)
        except ObjectNotFoundError:
            return False
        return True

    return bool(run_async(api, check))


# --- email change --------------------------------------------------------------------


def test_email_change_is_confirmed_from_the_new_address(sapi: Api) -> None:
    ada = sapi.signup("ada@example.com")
    sapi.signup("taken@example.com")
    url = "/api/v1/me/email"

    wrong = sapi.client.post(
        url, json={"new_email": "ada@new.example", "password": "nope-nope-nope"}, headers=ada
    )
    assert wrong.status_code == 401
    same = sapi.client.post(
        url, json={"new_email": "ADA@example.com", "password": PASSWORD}, headers=ada
    )
    assert same.status_code == 422

    # An address that already has an account: same answer, nothing to confirm.
    r = sapi.client.post(
        url, json={"new_email": "taken@example.com", "password": PASSWORD}, headers=ada
    )
    assert r.status_code == 202
    assert "already belongs to an account" in sapi.mail.to("taken@example.com")[-1].text
    assert (
        sql(sapi, "SELECT count(*) AS n FROM account_tokens WHERE purpose='email_change'")[0].n == 0
    )

    r = sapi.client.post(
        url, json={"new_email": "Ada@New.example", "password": PASSWORD}, headers=ada
    )
    assert r.status_code == 202, r.text
    assert sapi.me(ada)["email"] == "ada@example.com"  # nothing changes yet
    notice = sapi.mail.to("ada@example.com")[-1]
    assert "a***@new.example" in notice.text
    token = sapi.mail.last_token("ada@new.example", "/confirm-email-change")

    done = sapi.client.post("/api/v1/auth/email-change/confirm", json={"token": token})
    assert done.status_code == 200, done.text
    assert sapi.me(ada)["email"] == "ada@new.example"
    assert "now uses" in sapi.mail.to("ada@example.com")[-1].text
    assert sapi.login("ada@new.example").status_code == 200
    assert sapi.login("ada@example.com").status_code == 401
    again = sapi.client.post("/api/v1/auth/email-change/confirm", json={"token": token})
    assert again.status_code == 400
    actions = {r.action for r in sql(sapi, "SELECT action FROM audit_logs")}
    assert {"user.email_change_requested", "user.email_changed"} <= actions


def test_email_change_needs_a_fresh_2fa_check_when_2fa_is_on(sapi: Api) -> None:
    bob = sapi.signup("bob@example.com")
    secret, _ = sapi.enable_mfa(bob)
    body = {"new_email": "bob@new.example", "password": PASSWORD}
    r = sapi.client.post("/api/v1/me/email", json=body, headers=bob)
    assert r.json()["code"] == "mfa_step_up_required"
    r = sapi.client.post("/api/v1/me/email", json=body, headers=sapi.step_up(bob, secret))
    assert r.status_code == 202


# --- data export ---------------------------------------------------------------------


def test_data_export_contains_account_conversations_and_personal_files(
    sapi: Api, exports: list[uuid.UUID]
) -> None:
    eve = sapi.signup("eve@example.com", name="Eve")
    personal = ws(sapi, eve)
    ingest(sapi, personal, eve, POLICY, "policy.md")
    conv = create(sapi, personal, eve)
    answer = send(sapi, personal, conv["id"], eve, "Who reviews renewals?").json()
    mid = answer["assistant_message"]["id"]
    sapi.client.put(
        f"{base(personal)}/{conv['id']}/messages/{mid}/feedback",
        json={"rating": "down", "reasons": ["incomplete"], "comment": "Too short"},
        headers=eve,
    )
    sapi.create_org(eve, "Acme Labs")

    first = sapi.client.post("/api/v1/me/exports", headers=eve)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "pending"
    # Asking again while it's being prepared returns the same export.
    assert sapi.client.post("/api/v1/me/exports", headers=eve).json()["id"] == first.json()["id"]
    assert exports == [uuid.UUID(first.json()["id"])]
    not_yet = sapi.client.get(f"/api/v1/me/exports/{first.json()['id']}/download", headers=eve)
    assert not_yet.status_code == 404

    assert run_async(sapi, lambda c: data_export(exports[0], c)) == "ready"
    listed = sapi.client.get("/api/v1/me/exports", headers=eve).json()
    assert listed[0]["status"] == "ready"
    assert listed[0]["size_bytes"] > 0
    link = sapi.client.get(f"/api/v1/me/exports/{exports[0]}/download", headers=eve)
    assert link.status_code == 200, link.text
    assert "documind-export" in link.json()["url"]
    assert "ready" in sapi.mail.to("eve@example.com")[-1].subject

    # Someone else can't download it.
    mallory = sapi.signup("mallory@example.com")
    assert (
        sapi.client.get(f"/api/v1/me/exports/{exports[0]}/download", headers=mallory).status_code
        == 404
    )

    key = sql(sapi, "SELECT object_key FROM data_exports")[0].object_key

    async def read(c: Any) -> bytes:
        buf = io.BytesIO()
        await c.storage.download_to(key, buf)
        return buf.getvalue()

    archive = zipfile.ZipFile(io.BytesIO(run_async(sapi, read)))
    names = set(archive.namelist())
    assert "README.txt" in names
    account = json.loads(archive.read("account.json"))
    assert account["email"] == "eve@example.com"
    assert {w["name"] for w in account["workspaces"]} >= {"Acme Labs"}
    assert "totp_secret" not in json.dumps(account)
    folder = next(n.rsplit("/", 1)[0] for n in names if n.endswith("files/policy.md"))
    personal_folder = folder.removesuffix("/files")
    convs = json.loads(archive.read(f"{personal_folder}/conversations.json"))
    question, reply = convs[0]["messages"][:2]
    assert question["text"] == "Who reviews renewals?"
    assert reply["your_rating"]["comment"] == "Too short"
    assert archive.read(f"{folder}/policy.md") == POLICY
    docs = json.loads(archive.read(f"{personal_folder}/documents.json"))
    assert docs[0]["file_name"] == "policy.md"
    assert "workspaces/Acme Labs/conversations.json" in names

    # Expired archives are deleted by the sweep.
    sql(sapi, "UPDATE data_exports SET expires_at = now() - interval '1 minute' RETURNING id")
    assert run_async(sapi, sweep_data_exports) == 1
    assert not stored(sapi, key)
    gone = sapi.client.get(f"/api/v1/me/exports/{exports[0]}/download", headers=eve)
    assert gone.status_code == 404


# --- account deletion ----------------------------------------------------------------


def test_delete_account(sapi: Api) -> None:
    owner = sapi.signup("owner@example.com")
    org = sapi.create_org(owner)["id"]
    zoe = sapi.add_member(org, owner, "zoe@example.com", "admin")
    zoe_personal = ws(sapi, zoe)
    personal_doc = ingest(sapi, zoe_personal, zoe, POLICY, "mine.md")
    shared_doc = ingest(sapi, org, zoe, POLICY, "shared.md")
    conv = create(sapi, org, zoe)
    send(sapi, org, conv["id"], zoe, "Who reviews renewals?")
    personal_key = sql(
        sapi, "SELECT storage_key FROM document_versions WHERE document_id = :d", d=personal_doc
    )[0].storage_key
    assert stored(sapi, personal_key)

    url = "/api/v1/me"
    bad = sapi.client.request(
        "DELETE", url, json={"password": PASSWORD, "confirm": "delete"}, headers=zoe
    )
    assert bad.status_code == 422
    wrong = sapi.client.request(
        "DELETE", url, json={"password": "wrong-password!", "confirm": "DELETE"}, headers=zoe
    )
    assert wrong.status_code == 401

    # The only owner of an organization can't leave it ownerless.
    r = sapi.client.request(
        "DELETE", url, json={"password": PASSWORD, "confirm": "DELETE"}, headers=owner
    )
    assert r.status_code == 409
    assert r.json()["code"] == "last_owner"
    assert "Acme" in r.json()["detail"]

    r = sapi.client.request(
        "DELETE", url, json={"password": PASSWORD, "confirm": "DELETE"}, headers=zoe
    )
    assert r.status_code == 204, r.text
    assert sapi.login("zoe@example.com").status_code == 401
    assert sapi.client.get(url, headers=zoe).status_code == 401
    assert sql(sapi, "SELECT count(*) AS n FROM users WHERE email='zoe@example.com'")[0].n == 0
    ids = {str(r.id) for r in sql(sapi, "SELECT id FROM workspaces")}
    assert zoe_personal not in ids
    assert org in ids
    assert not stored(sapi, personal_key)
    # Her private conversation is gone; the document she shared stays with the organization.
    assert sql(sapi, "SELECT count(*) AS n FROM conversations")[0].n == 0
    kept = sql(sapi, "SELECT uploaded_by_user_id FROM documents WHERE id = :d", d=shared_doc)
    assert kept[0].uploaded_by_user_id is None
    assert "was deleted" in sapi.mail.to("zoe@example.com")[-1].subject
    assert "user.deleted" in {r.action for r in sql(sapi, "SELECT action FROM audit_logs")}


# --- organization deletion -----------------------------------------------------------


def test_delete_organization(sapi: Api) -> None:
    owner = sapi.signup("owner@example.com", name="Olive")
    org = sapi.create_org(owner, "Acme")["id"]
    member = sapi.add_member(org, owner, "member@example.com", "admin")
    ingest(sapi, org, owner, POLICY, "policy.md")
    key = sql(sapi, "SELECT storage_key FROM document_versions")[0].storage_key
    url = f"/api/v1/workspaces/{org}"
    body = {"confirm_name": "Acme"}

    assert sapi.client.request("DELETE", url, json=body, headers=member).status_code == 403
    no_2fa = sapi.client.request("DELETE", url, json=body, headers=owner)
    assert no_2fa.json()["code"] == "mfa_enrollment_required"
    elevated = sapi.elevated(owner)
    wrong = sapi.client.request("DELETE", url, json={"confirm_name": "acme"}, headers=elevated)
    assert wrong.status_code == 422
    personal = ws(sapi, owner)
    r = sapi.client.request("DELETE", f"/api/v1/workspaces/{personal}", json=body, headers=elevated)
    assert r.json()["code"] == "personal_workspace"

    r = sapi.client.request("DELETE", url, json=body, headers=elevated)
    assert r.status_code == 204, r.text
    assert sapi.client.get(f"{url}/documents", headers=member).status_code == 404
    assert sql(sapi, "SELECT count(*) AS n FROM organizations")[0].n == 0
    assert sql(sapi, "SELECT count(*) AS n FROM documents")[0].n == 0
    assert not stored(sapi, key)
    notice = sapi.mail.to("member@example.com")[-1]
    assert notice.subject == "Acme was deleted from DocuMind"
    assert "Olive" in notice.text
    # Members still have their own accounts and personal workspaces.
    assert sapi.login("member@example.com").status_code == 200
    assert "workspace.deleted" in {r.action for r in sql(sapi, "SELECT action FROM audit_logs")}
