"""Restricted collections hide their documents everywhere (ADR 0036)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import run_job

pytestmark = pytest.mark.integration

SECRET = b"# Salary bands\n\nThe engineering salary band for level five is confidential.\n"
OPEN = b"# Handbook\n\nThe office opens at nine. Bicycles go in the basement.\n"


def w(org: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{org}{tail}"


def setup(api: Api) -> dict[str, Any]:
    owner = api.signup("owner@example.com")
    org = str(api.create_org(owner, "Acme")["id"])
    hr = api.add_member(org, owner, "hr@example.com", "member")
    staff = api.add_member(org, owner, "staff@example.com", "member")
    col = api.client.post(w(org, "/collections"), json={"name": "HR"}, headers=owner).json()["id"]
    secret = upload(api, org, owner, SECRET, "salaries.md", collection_id=col).json()["id"]
    open_doc = upload(api, org, owner, OPEN, "handbook.md").json()["id"]
    for job in api.dispatcher.dispatched:
        run_job(api, job)
    hr_id = api.member_id(org, owner, "hr@example.com")
    members = api.client.get(w(org, "/members"), headers=owner).json()
    hr_user = next(m["user_id"] for m in members if m["id"] == hr_id)
    r = api.client.put(
        w(org, f"/collections/{col}/access"),
        json={"restricted": True, "member_user_ids": [hr_user]},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["restricted"] is True
    return {
        "org": org,
        "owner": owner,
        "hr": hr,
        "staff": staff,
        "col": col,
        "secret": secret,
        "open": open_doc,
        "hr_user": hr_user,
    }


def test_hidden_from_non_members_everywhere(api: Api) -> None:
    s = setup(api)
    org, staff, hr, owner = s["org"], s["staff"], s["hr"], s["owner"]
    secret, col = s["secret"], s["col"]

    def titles(h: dict[str, str]) -> set[str]:
        items = api.client.get(w(org, "/documents"), headers=h).json()["items"]
        return {d["title"] for d in items}

    assert titles(staff) == {"handbook"}
    assert titles(hr) == {"handbook", "salaries"}
    assert titles(owner) == {"handbook", "salaries"}  # admins see everything

    for tail in ("", "/download-url", "/pages", "/chunks", "/processing", "/versions"):
        r = api.client.get(w(org, f"/documents/{secret}{tail}"), headers=staff)
        assert r.status_code == 404, (tail, r.status_code)
        assert api.client.get(w(org, f"/documents/{secret}{tail}"), headers=hr).status_code == 200

    # Collections: the restricted one isn't listed or readable.
    names = {c["name"] for c in api.client.get(w(org, "/collections"), headers=staff).json()}
    assert names == set()
    assert api.client.get(w(org, f"/collections/{col}"), headers=staff).status_code == 404
    assert {c["name"] for c in api.client.get(w(org, "/collections"), headers=hr).json()} == {"HR"}

    # Search and answers never use it.
    q = {"query": "engineering salary band level five"}
    hits = api.client.post(w(org, "/search"), json=q, headers=staff).json()["results"]
    assert all(h["document_id"] != secret for h in hits)
    hits = api.client.post(w(org, "/search"), json=q, headers=hr).json()["results"]
    assert any(h["document_id"] == secret for h in hits)
    asked = api.client.post(
        w(org, "/ask"),
        json={"question": "What is the salary band?", "document_ids": [secret]},
        headers=staff,
    )
    assert asked.status_code == 404
    answer = api.client.post(
        w(org, "/ask"), json={"question": "What is the engineering salary band?"}, headers=staff
    ).json()
    assert all(c["document_id"] != secret for c in answer["citations"])
    summary = api.client.post(w(org, f"/documents/{secret}/summary"), json={}, headers=staff)
    assert summary.status_code == 404

    # Extraction and deadlines.
    r = api.client.post(
        w(org, "/extractions"),
        json={"template": "obligations", "document_ids": [secret]},
        headers=staff,
    )
    assert r.status_code == 404
    assert (
        api.client.post(
            w(org, "/extractions"),
            json={"template": "obligations", "collection_id": col},
            headers=staff,
        ).status_code
        == 404
    )
    api.client.post(
        w(org, "/deadlines"),
        json={"title": "Review bands", "due_date": "2030-01-01", "document_id": secret},
        headers=owner,
    )
    assert api.client.get(w(org, "/deadlines"), headers=staff).json() == []
    assert len(api.client.get(w(org, "/deadlines"), headers=hr).json()) == 1

    # A duplicate upload doesn't reveal the hidden title.
    dup = upload(api, org, staff, SECRET, "copy.md")
    assert dup.status_code == 409
    assert "salaries" not in dup.json()["detail"]

    # Opening the collection again makes it visible.
    api.client.put(w(org, f"/collections/{col}/access"), json={"restricted": False}, headers=owner)
    assert titles(staff) == {"handbook", "salaries"}


def test_only_admins_manage_access(api: Api) -> None:
    s = setup(api)
    org, col = s["org"], s["col"]
    r = api.client.put(
        w(org, f"/collections/{col}/access"), json={"restricted": False}, headers=s["hr"]
    )
    assert r.status_code == 403
    outsider = api.signup("out@example.com")
    out_id = api.client.get("/api/v1/me", headers=outsider).json()["id"]
    r = api.client.put(
        w(org, f"/collections/{col}/access"),
        json={"restricted": True, "member_user_ids": [out_id]},
        headers=s["owner"],
    )
    assert r.json()["code"] == "invalid_members"
    access = api.client.get(w(org, f"/collections/{col}/access"), headers=s["owner"]).json()
    assert access == {"restricted": True, "member_user_ids": [s["hr_user"]]}
