"""Sample documents for trying DocuMind (ADR 0033)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import run_job, sql, ws
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


def samples(w: str) -> str:
    return f"/api/v1/workspaces/{w}/samples"


def load(api: Api, w: str, h: dict[str, str]) -> Any:
    before = len(api.dispatcher.dispatched)
    r = api.client.post(samples(w), headers=h)
    assert r.status_code == 201, r.text
    for job in api.dispatcher.dispatched[before:]:
        assert run_job(api, job).status == "succeeded"
    return r.json()


def test_samples_load_answer_and_remove(api: Api) -> None:
    h = api.signup("new@example.com")
    w = ws(api, h)
    assert api.client.get(samples(w), headers=h).json()["loaded"] is False

    body = load(api, w, h)
    assert body["loaded"] is True
    assert len(body["document_ids"]) == 3
    assert body["collection_id"]
    assert any("non-renewal" in q for q in body["questions"])
    docs = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()
    assert docs["total"] == 3
    assert all(d["is_sample"] and d["status"] == "ready" for d in docs["items"])
    collection = api.client.get(f"/api/v1/workspaces/{w}/collections", headers=h).json()
    assert [c["name"] for c in collection] == ["Sample documents"]
    assert collection[0]["document_count"] == 3

    # A real answer from the samples, with a citation.
    answer = api.client.post(
        f"/api/v1/workspaces/{w}/ask",
        json={"question": "What notice is needed for non-renewal of the agreement?"},
        headers=h,
    ).json()
    assert answer["status"] == "grounded", answer
    assert "90 days" in answer["answer"]
    assert answer["citations"][0]["document_name"] == "Sample: Master Services Agreement"

    # Idempotent: nothing is added twice.
    before = len(api.dispatcher.dispatched)
    assert api.client.post(samples(w), headers=h).status_code == 201
    assert len(api.dispatcher.dispatched) == before

    # Removing needs no 2FA (the owner here has none) and takes the collection too.
    removed = api.client.delete(samples(w), headers=h).json()
    assert removed == {"removed": 3}
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 0
    assert api.client.get(f"/api/v1/workspaces/{w}/collections", headers=h).json() == []
    actions = [
        r.action
        for r in sql(api, "SELECT action FROM audit_logs WHERE workspace_id = :w", w=uuid.UUID(w))
    ]
    assert actions.count("document.sample_removed") == 3


def test_changed_samples_are_kept_and_permissions_apply(api: Api) -> None:
    owner = api.signup("owner@example.com")
    org = str(api.create_org(owner, "Acme")["id"])
    viewer = api.add_member(org, owner, "vic@example.com", "viewer")
    member = api.add_member(org, owner, "mo@example.com", "member")
    assert api.client.post(samples(org), headers=viewer).status_code == 403
    assert api.client.get(samples(org), headers=viewer).status_code == 200
    body = load(api, org, member)
    agreement = body["document_ids"][0]

    # A user uploads a new version of one sample: it's theirs now and stays.
    r = api.client.post(
        f"/api/v1/workspaces/{org}/documents/{agreement}/versions",
        files={"file": ("services-agreement.md", b"# Our own terms\n\nNet 45.\n", "text/markdown")},
        headers=member,
    )
    assert r.status_code == 201, r.text
    one = api.client.get(f"/api/v1/workspaces/{org}/documents/{agreement}", headers=member).json()
    assert one["is_sample"] is False
    assert api.client.delete(samples(org), headers=viewer).status_code == 403
    assert api.client.delete(samples(org), headers=member).json() == {"removed": 2}
    remaining = api.client.get(f"/api/v1/workspaces/{org}/documents", headers=member).json()
    assert [d["id"] for d in remaining["items"]] == [agreement]
    # The collection keeps the changed document, so it stays.
    cols = api.client.get(f"/api/v1/workspaces/{org}/collections", headers=member).json()
    assert cols[0]["document_count"] == 1


def test_samples_respect_plan_quotas(api: Api) -> None:
    h = api.signup("tight@example.com")
    w = ws(api, h)
    assign(api, w, "free", max_documents=2)
    upload(api, w, h, b"# Mine\n\nMy own notes.\n", "mine.md")
    r = api.client.post(samples(w), headers=h)
    assert r.status_code == 402
    assert r.json()["code"] == "quota_exceeded"
    total = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"]
    assert total == 2  # one sample got in before the limit


def test_every_suggested_question_is_answered_from_the_samples(api: Api) -> None:
    """Even the offline development model must answer them, with a citation."""
    h = api.signup("asker@example.com")
    w = ws(api, h)
    body = load(api, w, h)
    unanswered = []
    for question in body["questions"]:
        answer = api.client.post(
            f"/api/v1/workspaces/{w}/ask", json={"question": question}, headers=h
        ).json()
        if answer["status"] != "grounded" or not answer["citations"]:
            unanswered.append(question)
    assert unanswered == []
