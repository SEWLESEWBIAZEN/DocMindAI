"""Answer feedback and the admin quality summary (ADR 0038)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_conversations_api import base, create, send
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_rag_api import POLICY, SpyLLM, ingest

pytestmark = pytest.mark.integration


@pytest.fixture
def chat(make_api: Any) -> Api:
    api: Api = make_api({"llm_provider": SpyLLM()})
    return api


def ask(api: Api, org: str, who: dict[str, str], question: str) -> tuple[str, str]:
    conv = create(api, org, who)
    r = send(api, org, conv["id"], who, question)
    assert r.status_code == 201, r.text
    return conv["id"], r.json()["assistant_message"]["id"]


def feedback(api: Api, org: str, cid: str, mid: str) -> str:
    return f"{base(org)}/{cid}/messages/{mid}/feedback"


def test_members_rate_their_answers_and_admins_see_counts_and_shared_ones(chat: Api) -> None:
    owner = chat.signup("owner@example.com")
    org = chat.create_org(owner)["id"]
    member = chat.add_member(org, owner, "member@example.com", "member")
    ingest(chat, org, owner, POLICY, "policy.md")

    # A member rates an answer down, with reasons and a comment, and shares it.
    cid, mid = ask(chat, org, member, "Who reviews renewals?")
    r = chat.client.put(
        feedback(chat, org, cid, mid),
        json={
            "rating": "down",
            "reasons": ["incomplete", "bad_citation", "incomplete"],
            "comment": "  Missed the second reviewer.  ",
            "shared": True,
        },
        headers=member,
    )
    assert r.status_code == 200, r.text
    rated = r.json()["feedback"]
    assert rated["rating"] == "down"
    assert rated["reasons"] == ["incomplete", "bad_citation"]
    assert rated["comment"] == "Missed the second reviewer."
    # The rating comes back with the conversation's messages.
    page = chat.client.get(f"{base(org)}/{cid}/messages", headers=member).json()["items"]
    assert [m["feedback"] for m in page if m["role"] == "user"] == [None]
    assert [m["feedback"]["rating"] for m in page if m["role"] == "assistant"] == ["down"]

    # Rating again replaces it; reasons only belong to a thumbs down.
    r = chat.client.put(
        feedback(chat, org, cid, mid),
        json={"rating": "up", "reasons": ["incorrect"], "shared": True},
        headers=member,
    )
    assert r.json()["feedback"] == {**r.json()["feedback"], "rating": "up", "reasons": []}
    assert sql(chat, "SELECT count(*) AS n FROM message_feedback")[0].n == 1
    chat.client.put(
        feedback(chat, org, cid, mid),
        json={
            "rating": "down",
            "reasons": ["incomplete"],
            "comment": "Missed one.",
            "shared": True,
        },
        headers=member,
    )

    # A private thumbs up from the owner: counted, never shown.
    cid2, mid2 = ask(chat, org, owner, "What is the renewal notice?")
    chat.client.put(feedback(chat, org, cid2, mid2), json={"rating": "up"}, headers=owner)

    quality = chat.client.get(f"/api/v1/workspaces/{org}/answer-quality", headers=owner)
    assert quality.status_code == 200, quality.text
    q = quality.json()
    assert (q["answers"], q["rated"], q["up"], q["down"]) == (2, 2, 1, 1)
    assert q["reasons"]["incomplete"] == 1
    assert q["reasons"]["incorrect"] == 0
    assert sum(m["up"] + m["down"] for m in q["models"]) == 2
    assert len(q["shared"]) == 1
    shared = q["shared"][0]
    assert shared["question"] == "Who reviews renewals?"
    assert shared["comment"] == "Missed one."
    assert shared["answer"]
    assert "What is the renewal notice?" not in quality.text  # not shared
    assert "member@example.com" not in quality.text  # who rated is never shown

    # Members can't read the summary; nobody can rate someone else's answer.
    assert (
        chat.client.get(f"/api/v1/workspaces/{org}/answer-quality", headers=member).status_code
        == 403
    )
    r = chat.client.put(feedback(chat, org, cid, mid), json={"rating": "up"}, headers=owner)
    assert r.status_code == 404

    # Questions can't be rated; bad input is refused.
    user_msg = next(m["id"] for m in page if m["role"] == "user")
    r = chat.client.put(feedback(chat, org, cid, user_msg), json={"rating": "up"}, headers=member)
    assert r.status_code == 404
    for body in (
        {"rating": "meh"},
        {"rating": "down", "reasons": ["rude"]},
        {"rating": "down", "comment": "x" * 1001},
        {"rating": "up", "extra": 1},
    ):
        r = chat.client.put(feedback(chat, org, cid, mid), json=body, headers=member)
        assert r.status_code == 422, body

    # Removing the rating; deleting the conversation removes it too.
    r = chat.client.delete(feedback(chat, org, cid2, mid2), headers=owner)
    assert r.status_code == 204
    assert chat.client.get(f"{base(org)}/{cid}", headers=member).status_code == 200
    assert chat.client.delete(f"{base(org)}/{cid}", headers=member).status_code == 204
    assert sql(chat, "SELECT count(*) AS n FROM message_feedback")[0].n == 0


def test_personal_workspace_owner_sees_their_own_summary(chat: Api) -> None:
    ada = chat.signup("ada@example.com")
    personal = ws(chat, ada)
    ingest(chat, personal, ada, POLICY, "policy.md")
    cid, mid = ask(chat, personal, ada, "Who reviews renewals?")
    chat.client.put(
        feedback(chat, personal, cid, mid),
        json={"rating": "down", "reasons": ["missed_document"]},
        headers=ada,
    )
    q = chat.client.get(
        f"/api/v1/workspaces/{personal}/answer-quality", params={"days": 7}, headers=ada
    ).json()
    assert (q["days"], q["down"], q["reasons"]["missed_document"], q["shared"]) == (7, 1, 1, [])


def test_eval_command_scores_the_sample_set(
    api: Api, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """``documind-manage eval``: the built-in questions over the sample documents."""
    import asyncio
    import json

    from documind.cli import _eval
    from documind.container import build_container
    from tests.integration.test_onboarding_api import load

    owner = api.signup("eve@example.com")
    w = ws(api, owner)
    load(api, w, owner)

    async def run(**kwargs: Any) -> int:
        c = build_container(api.settings)
        try:
            return await _eval(c, **kwargs)
        finally:
            await c.aclose()

    out = tmp_path / "report.json"
    code = asyncio.run(
        run(
            workspace_id=uuid.UUID(w),
            source="samples",
            as_email=None,
            json_out=str(out),
            markdown_out=str(tmp_path / "report.md"),
            min_pass=0.8,
        )
    )
    printed = capsys.readouterr()
    report = json.loads(out.read_text())
    assert code == 0, printed.out
    assert report["total"] == 16
    assert report["pass_rate"] >= 0.8
    assert "PASS  msa-non-renewal" in printed.err
    assert (tmp_path / "report.md").read_text().startswith("# Sample documents:")
    # Nothing is saved: evaluation doesn't create conversations.
    assert sql(api, "SELECT count(*) AS n FROM conversations")[0].n == 0

    # A stricter bar than the result fails the run; unknown members are refused.
    assert (
        asyncio.run(
            run(
                workspace_id=uuid.UUID(w),
                source="samples",
                as_email=None,
                json_out=None,
                markdown_out=None,
                min_pass=1.01,
            )
        )
        == 1
    )
    assert (
        asyncio.run(
            run(
                workspace_id=uuid.UUID(w),
                source="samples",
                as_email="nobody@example.com",
                json_out=None,
                markdown_out=None,
                min_pass=0.0,
            )
        )
        == 2
    )
