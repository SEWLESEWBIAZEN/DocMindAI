"""Usage summary endpoint."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_conversations_api import create, send
from tests.integration.test_processing_pipeline import contract_pdf, ws
from tests.integration.test_rag_api import POLICY, ingest

pytestmark = pytest.mark.integration


def test_usage_summary_counts_documents_processing_and_ai(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ws(api, h)
    ingest(api, w, h, POLICY, "policy.md")
    ingest(api, w, h, contract_pdf(), "msa.pdf")
    conv = create(api, w, h)
    send(api, w, conv["id"], h, "Who reviews renewals?")
    send(api, w, conv["id"], h, "And the notice period?")

    r = api.client.get(f"/api/v1/workspaces/{w}/usage", params={"days": 7}, headers=h)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    docs = body["documents"]
    assert (docs["total"], docs["ready"], docs["failed"], docs["archived"]) == (2, 2, 0, 0)
    assert docs["storage_bytes"] > len(POLICY)
    assert docs["pages"] == 2  # the PDF; Markdown is unpaginated
    assert docs["chunks"] >= 2
    assert body["processing"]["jobs_succeeded"] == 2
    assert body["processing"]["embedding_tokens"] > 0
    ai = body["ai"]
    assert (ai["conversations"], ai["questions"], ai["answers_completed"]) == (1, 2, 2)
    assert ai["llm_output_tokens"] > 0
    assert ai["query_embedding_tokens"] > 0
    assert len(body["daily"]) == 7
    today = body["daily"][-1]
    assert today["questions"] == 2
    assert today["documents_processed"] == 2
    assert body["members"][0]["questions"] == 2


def test_usage_requires_usage_permission(api: Api) -> None:
    owner = api.signup("owner@example.com")
    org = api.create_org(owner)["id"]
    member = api.add_member(org, owner, "member@example.com", "member")
    admin = api.add_member(org, owner, "admin@example.com", "admin")
    url = f"/api/v1/workspaces/{org}/usage"
    assert api.client.get(url, headers=member).status_code == 403
    assert api.client.get(url, headers=admin).status_code == 200
    outsider = api.signup("outsider@example.com")
    assert api.client.get(url, headers=outsider).status_code == 404
    assert api.client.get(url, params={"days": 0}, headers=owner).status_code == 422
