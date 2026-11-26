"""Conversations against real Postgres/pgvector, Redis and MinIO (TestClient)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

from documind.application.conversations.sweeper import StaleReplySweeper
from documind.application.ports import ChatMessage, LLMDelta, LLMResult, LLMStreamEvent
from documind.container import build_container
from documind.domain.processing.errors import TransientProcessingError
from documind.domain.rag.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import contract_pdf, sql, ws
from tests.integration.test_rag_api import POLICY, SpyLLM, ingest

pytestmark = pytest.mark.integration

SSE = {"Accept": "text/event-stream"}


def base(workspace: str) -> str:
    return f"/api/v1/workspaces/{workspace}/conversations"


def create(api: Api, workspace: str, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    r = api.client.post(base(workspace), json=body, headers=headers)
    assert r.status_code == 201, r.text
    result: dict[str, Any] = r.json()
    return result


def send(
    api: Api, workspace: str, cid: str, headers: dict[str, str], content: str, *, sse: bool = False
) -> Any:
    return api.client.post(
        f"{base(workspace)}/{cid}/messages",
        json={"content": content},
        headers={**headers, **SSE} if sse else headers,
    )


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


class FailingStreamLLM(SpyLLM):
    def stream(self, messages: list[ChatMessage], **_: Any) -> AsyncIterator[LLMStreamEvent]:
        async def gen() -> AsyncIterator[LLMStreamEvent]:
            yield LLMDelta("Partial ")
            raise TransientProcessingError("down", code="llm_unavailable")

        return gen()

    async def generate(self, messages: list[ChatMessage], **_: Any) -> LLMResult:
        raise TransientProcessingError("down", code="llm_unavailable")


@pytest.fixture
def spy() -> SpyLLM:
    return SpyLLM()


@pytest.fixture
def chat(make_api: Any, spy: SpyLLM) -> Api:
    api: Api = make_api({"llm_provider": spy})
    return api


@pytest.fixture
def ada(chat: Api) -> tuple[dict[str, str], str]:
    headers = chat.signup("ada@example.com")
    return headers, ws(chat, headers)


# --- lifecycle -------------------------------------------------------------------


def test_conversation_lifecycle(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    msa = ingest(chat, w, h, contract_pdf(), "msa.pdf")
    conv = create(chat, w, h)
    assert (conv["title"], conv["title_source"], conv["scope"]) == (
        "New conversation",
        "auto",
        "all",
    )

    r = send(chat, w, conv["id"], h, "How much notice is needed before non-renewal?")
    assert r.status_code == 201, r.text
    body = r.json()
    user, answer = body["user_message"], body["assistant_message"]
    assert (user["role"], user["status"], user["sequence"]) == ("user", "completed", 1)
    assert (answer["role"], answer["status"], answer["sequence"]) == ("assistant", "completed", 2)
    assert answer["answer_status"] == "grounded"
    assert answer["reply_to_id"] == user["id"]
    assert answer["prompt_version"] == PROMPT_VERSION
    assert answer["citations"][0]["document_id"] == msa
    assert answer["citations"][0]["document_available"] is True
    assert "ninety days" in answer["content"]

    detail = chat.client.get(f"{base(w)}/{conv['id']}", headers=h).json()
    assert detail["title"] == "How much notice is needed before non-renewal?"
    assert detail["message_count"] == 2

    renamed = chat.client.patch(
        f"{base(w)}/{conv['id']}", json={"title": " MSA  notice "}, headers=h
    )
    assert renamed.json()["title"] == "MSA notice"
    assert renamed.json()["title_source"] == "user"

    page = chat.client.get(f"{base(w)}/{conv['id']}/messages", headers=h).json()
    assert [m["role"] for m in page["items"]] == ["user", "assistant"]
    assert page["has_more"] is False
    cites = chat.client.get(
        f"{base(w)}/{conv['id']}/messages/{answer['id']}/citations", headers=h
    ).json()
    assert cites == answer["citations"]

    listed = chat.client.get(base(w), headers=h).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == conv["id"]
    assert chat.client.get(base(w), params={"q": "notice"}, headers=h).json()["total"] == 1
    assert chat.client.get(base(w), params={"q": "100%_"}, headers=h).json()["total"] == 0

    assert chat.client.delete(f"{base(w)}/{conv['id']}", headers=h).status_code == 204
    assert chat.client.get(f"{base(w)}/{conv['id']}", headers=h).status_code == 404
    assert sql(chat, "SELECT count(*) FROM messages")[0][0] == 0
    audit = sql(chat, "SELECT action FROM audit_logs WHERE action = 'conversation.deleted'")
    assert len(audit) == 1


def test_streams_server_sent_events(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    conv = create(chat, w, h)
    r = send(chat, w, conv["id"], h, "Which renewals require Legal review?", sse=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"].startswith("no-cache")
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    assert names[0] == "message.created"
    assert names[-1] == "answer.completed"
    assert "answer.delta" in names
    created = events[0][1]
    assert created["assistant_message"]["status"] == "streaming"
    streamed = "".join(d["text"] for n, d in events if n == "answer.delta")
    final = events[-1][1]["message"]
    assert final["status"] == "completed"
    assert final["content"] == streamed.strip()
    assert final["citations"]
    stored = chat.client.get(f"{base(w)}/{conv['id']}/messages/{final['id']}", headers=h).json()
    assert stored["status"] == "completed"
    assert stored["citations"] == final["citations"]


def test_follow_up_sends_history_and_condensed_query(
    chat: Api, spy: SpyLLM, ada: tuple[dict[str, str], str]
) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    conv = create(chat, w, h)
    first = send(chat, w, conv["id"], h, "Which vendor renewals require Legal review?").json()
    spy.calls.clear()
    second = send(chat, w, conv["id"], h, "And what about the security questionnaire?").json()
    answer_messages = spy.calls[-1]
    assert answer_messages[0] == ChatMessage("system", SYSTEM_PROMPT)
    assert answer_messages[1] == ChatMessage("user", "Which vendor renewals require Legal review?")
    assert answer_messages[2].role == "assistant"
    assert "[1]" not in answer_messages[2].content  # markers of earlier answers removed
    assert "<question>\nAnd what about the security questionnaire?" in answer_messages[3].content
    # The local provider can't condense, so the fallback query includes the earlier question.
    query = sql(
        chat,
        "SELECT retrieval_query FROM messages WHERE id = :id",
        id=uuid.UUID(second["assistant_message"]["id"]),
    )[0][0]
    assert query.startswith("Which vendor renewals require Legal review?")
    assert second["assistant_message"]["sequence"] == 4
    assert first["assistant_message"]["status"] == "completed"


# --- sources ---------------------------------------------------------------------


def test_selected_sources_limit_answers(
    chat: Api, spy: SpyLLM, ada: tuple[dict[str, str], str]
) -> None:
    h, w = ada
    msa = ingest(chat, w, h, contract_pdf(), "msa.pdf")
    policy = ingest(chat, w, h, POLICY, "policy.md")
    question = "What notice is needed for non-renewal, and which renewals require Legal review?"

    conv = create(chat, w, h, document_ids=[policy])
    assert conv["scope"] == "selected"
    assert [d["id"] for d in conv["documents"]] == [policy]
    cited = send(chat, w, conv["id"], h, question).json()["assistant_message"]["citations"]
    assert {c["document_id"] for c in cited} == {policy}

    col = chat.client.post(f"/api/v1/workspaces/{w}/collections", json={"name": "C"}, headers=h)
    col_id = col.json()["id"]
    chat.client.put(f"/api/v1/workspaces/{w}/collections/{col_id}/documents/{msa}", headers=h)
    updated = chat.client.put(
        f"{base(w)}/{conv['id']}/sources", json={"collection_ids": [col_id]}, headers=h
    ).json()
    assert updated["collections"] == [{"id": col_id, "name": "C"}]
    assert updated["document_ids"] == []
    cited = send(chat, w, conv["id"], h, question).json()["assistant_message"]["citations"]
    assert {c["document_id"] for c in cited} == {msa}

    both = chat.client.put(
        f"{base(w)}/{conv['id']}/sources",
        json={"document_ids": [policy], "collection_ids": [col_id]},
        headers=h,
    ).json()
    assert both["scope"] == "selected"
    cited = send(chat, w, conv["id"], h, question).json()["assistant_message"]["citations"]
    assert {c["document_id"] for c in cited} == {msa, policy}  # union

    cleared = chat.client.put(f"{base(w)}/{conv['id']}/sources", json={}, headers=h).json()
    assert cleared["scope"] == "all"


def test_deleted_selection_never_widens_to_the_workspace(
    chat: Api, spy: SpyLLM, ada: tuple[dict[str, str], str]
) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    selected = ingest(chat, w, h, contract_pdf(), "msa.pdf")
    conv = create(chat, w, h, document_ids=[selected])
    sql(chat, "DELETE FROM documents WHERE id = :id", id=uuid.UUID(selected))
    spy.calls.clear()
    answer = send(chat, w, conv["id"], h, "Which renewals require Legal review?").json()
    answer = answer["assistant_message"]
    assert answer["answer_status"] == "insufficient_context"
    assert answer["citations"] == []
    assert [x["code"] for x in answer["warnings"]] == ["selection_empty"]
    assert len(spy.calls) <= 1  # only the follow-up condenser (no history: none at all)


def test_archived_or_deleted_cited_documents_stay_readable(
    chat: Api, ada: tuple[dict[str, str], str]
) -> None:
    h, w = ada
    msa = ingest(chat, w, h, contract_pdf(), "msa.pdf")
    conv = create(chat, w, h)
    answer = send(chat, w, conv["id"], h, "notice before non-renewal").json()["assistant_message"]
    url = f"{base(w)}/{conv['id']}/messages/{answer['id']}"
    chat.client.post(f"/api/v1/workspaces/{w}/documents/{msa}/archive", headers=h)
    cite = chat.client.get(url, headers=h).json()["citations"][0]
    assert cite["document_available"] is False
    sql(chat, "DELETE FROM documents WHERE id = :id", id=uuid.UUID(msa))
    cite = chat.client.get(url, headers=h).json()["citations"][0]
    assert cite["document_id"] is None
    assert cite["chunk_id"] is None
    assert "ninety days" in cite["excerpt"]  # snapshot kept


# --- regenerate --------------------------------------------------------------------


def test_regenerate_latest_answer_only(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    conv = create(chat, w, h)
    first = send(chat, w, conv["id"], h, "Who reviews large renewals?").json()
    second = send(chat, w, conv["id"], h, "Who handles security reviews?").json()
    old = first["assistant_message"]["id"]
    latest = second["assistant_message"]["id"]
    regen_url = f"{base(w)}/{conv['id']}/messages/{{}}/regenerate"

    assert chat.client.post(regen_url.format(old), headers=h).json()["code"] == "not_latest_answer"
    user_id = second["user_message"]["id"]
    assert chat.client.post(regen_url.format(user_id), headers=h).json()["code"] == "not_an_answer"

    r = chat.client.post(regen_url.format(latest), headers={**h, **SSE})
    events = parse_sse(r.text)
    assert events[0][1]["user_message"] is None
    new = events[-1][1]["message"]
    assert new["reply_to_id"] == user_id
    assert new["sequence"] == 5

    visible = chat.client.get(f"{base(w)}/{conv['id']}/messages", headers=h).json()["items"]
    assert [m["id"] for m in visible if m["role"] == "assistant"] == [old, new["id"]]
    everything = chat.client.get(
        f"{base(w)}/{conv['id']}/messages", params={"include_superseded": True}, headers=h
    ).json()["items"]
    superseded = next(m for m in everything if m["id"] == latest)
    assert superseded["superseded"] is True
    # The superseded answer can't be regenerated again.
    assert chat.client.post(regen_url.format(latest), headers=h).status_code == 409


# --- failures, concurrency, recovery --------------------------------------------------


def test_model_failure_is_recorded_not_completed(make_api: Any) -> None:
    api: Api = make_api({"llm_provider": FailingStreamLLM()})
    h = api.signup("ada@example.com")
    w = ws(api, h)
    ingest(api, w, h, POLICY, "policy.md")
    conv = create(api, w, h)
    r = send(api, w, conv["id"], h, "Which renewals require Legal review?")
    assert r.status_code == 503
    assert r.json()["code"] == "llm_unavailable"
    events = parse_sse(send(api, w, conv["id"], h, "Who reviews renewals?", sse=True).text)
    name, data = events[-1]
    assert name == "answer.error"
    assert data["code"] == "llm_unavailable"
    assert data["message"]["status"] == "failed"
    assert data["message"]["content"] == "Partial "
    statuses = [row[0] for row in sql(api, "SELECT status FROM messages WHERE role='assistant'")]
    assert statuses == ["failed", "failed"]


def _insert_streaming(api: Api, conversation_id: str, age_seconds: int) -> uuid.UUID:
    mid = uuid.uuid4()
    sql(
        api,
        "INSERT INTO messages (id, workspace_id, conversation_id, sequence, role, content, status,"
        " created_at, updated_at) SELECT :id, workspace_id, id, 99, 'assistant', 'half', "
        "'streaming', now() - make_interval(secs => :age), now() - make_interval(secs => :age) "
        "FROM conversations WHERE id = :cid",
        id=mid,
        cid=uuid.UUID(conversation_id),
        age=age_seconds,
    )
    return mid


def test_one_answer_at_a_time_and_stale_recovery(
    chat: Api, ada: tuple[dict[str, str], str]
) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    conv = create(chat, w, h)
    running = _insert_streaming(chat, conv["id"], age_seconds=5)
    r = send(chat, w, conv["id"], h, "Who reviews renewals?")
    assert r.status_code == 409
    assert r.json()["code"] == "reply_in_progress"

    sql(
        chat,
        "UPDATE messages SET updated_at = now() - interval '1 hour' WHERE id = :id",
        id=running,
    )
    assert send(chat, w, conv["id"], h, "Who reviews renewals?").status_code == 201
    status, code = sql(chat, "SELECT status, error_code FROM messages WHERE id = :id", id=running)[
        0
    ]
    assert (status, code) == ("failed", "interrupted")


def test_stale_reply_sweeper(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    conv = create(chat, w, h)
    stale = _insert_streaming(chat, conv["id"], age_seconds=3600)

    async def go() -> int:
        c = build_container(chat.settings)
        try:
            return await StaleReplySweeper(c.session_factory, stale_after_seconds=300).sweep()
        finally:
            await c.aclose()

    assert asyncio.run(go()) == 1
    assert sql(chat, "SELECT status FROM messages WHERE id = :id", id=stale)[0][0] == "failed"


def test_cancel_endpoint(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    ingest(chat, w, h, POLICY, "policy.md")
    conv = create(chat, w, h)
    done = send(chat, w, conv["id"], h, "Who reviews renewals?").json()["assistant_message"]
    url = f"{base(w)}/{conv['id']}/messages/{{}}/cancel"
    assert chat.client.post(url.format(done["id"]), headers=h).json()["code"] == "not_streaming"

    running = _insert_streaming(chat, conv["id"], age_seconds=1)
    r = chat.client.post(url.format(running), headers=h)
    assert r.status_code == 202
    assert r.json()["status"] == "streaming"

    async def flag() -> int:
        redis = Redis.from_url(chat.settings.redis_url.get_secret_value())
        try:
            return int(await redis.exists(f"chat:cancel:{running}"))
        finally:
            await redis.aclose()

    assert asyncio.run(flag()) == 1


# --- security ----------------------------------------------------------------------


def test_conversations_are_private_and_workspace_scoped(chat: Api) -> None:
    owner = chat.signup("owner@example.com")
    org = chat.create_org(owner)["id"]
    colleague = chat.add_member(org, owner, "colleague@example.com", "admin")
    outsider = chat.signup("outsider@example.com")
    ingest(chat, org, owner, POLICY, "policy.md")
    conv = create(chat, org, owner)
    answer = send(chat, org, conv["id"], owner, "Who reviews renewals?").json()["assistant_message"]
    cid, mid = conv["id"], answer["id"]
    paths = [
        ("get", f"{base(org)}/{cid}", None),
        ("patch", f"{base(org)}/{cid}", {"title": "x"}),
        ("put", f"{base(org)}/{cid}/sources", {}),
        ("delete", f"{base(org)}/{cid}", None),
        ("get", f"{base(org)}/{cid}/messages", None),
        ("get", f"{base(org)}/{cid}/messages/{mid}", None),
        ("get", f"{base(org)}/{cid}/messages/{mid}/citations", None),
        ("post", f"{base(org)}/{cid}/messages", {"content": "hi"}),
        ("post", f"{base(org)}/{cid}/messages/{mid}/regenerate", None),
        ("post", f"{base(org)}/{cid}/messages/{mid}/cancel", None),
        ("put", f"{base(org)}/{cid}/messages/{mid}/feedback", {"rating": "up"}),
        ("delete", f"{base(org)}/{cid}/messages/{mid}/feedback", None),
    ]
    for who in (colleague, outsider):
        for method, path, body in paths:
            kwargs: dict[str, Any] = {"headers": who}
            if body is not None:
                kwargs["json"] = body
            r = getattr(chat.client, method)(path, **kwargs)
            assert r.status_code == 404, (method, path, r.status_code)
    assert chat.client.get(base(org), headers=colleague).json()["total"] == 0

    # Through another workspace id, even the owner's own conversation is not found.
    personal = ws(chat, owner)
    assert chat.client.get(f"{base(personal)}/{cid}", headers=owner).status_code == 404
    assert chat.client.get(f"{base(org)}/{cid}", headers=owner).status_code == 200


def test_sources_must_belong_to_the_workspace(chat: Api) -> None:
    ada = chat.signup("ada@example.com")
    eve = chat.signup("eve@example.com")
    w_ada, w_eve = ws(chat, ada), ws(chat, eve)
    theirs = ingest(chat, w_eve, eve, POLICY, "policy.md")
    col = chat.client.post(
        f"/api/v1/workspaces/{w_eve}/collections", json={"name": "X"}, headers=eve
    )
    for body in ({"document_ids": [theirs]}, {"collection_ids": [col.json()["id"]]}):
        r = chat.client.post(base(w_ada), json=body, headers=ada)
        assert r.status_code == 404
    conv = create(chat, w_ada, ada)
    r = chat.client.put(
        f"{base(w_ada)}/{conv['id']}/sources", json={"document_ids": [theirs]}, headers=ada
    )
    assert r.status_code == 404


def test_viewer_can_hold_conversations(chat: Api) -> None:
    owner = chat.signup("owner@example.com")
    org = chat.create_org(owner)["id"]
    viewer = chat.add_member(org, owner, "viewer@example.com", "viewer")
    ingest(chat, org, owner, POLICY, "policy.md")
    conv = create(chat, org, viewer)
    assert send(chat, org, conv["id"], viewer, "Who reviews renewals?").status_code == 201


@pytest.mark.parametrize(
    "body", [{"content": ""}, {"content": "x" * 2001}, {"content": "ok", "extra": 1}, {}]
)
def test_message_validation(chat: Api, ada: tuple[dict[str, str], str], body: Any) -> None:
    h, w = ada
    conv = create(chat, w, h)
    r = chat.client.post(f"{base(w)}/{conv['id']}/messages", json=body, headers=h)
    assert r.status_code == 422


def test_blank_message_rejected(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    conv = create(chat, w, h)
    r = send(chat, w, conv["id"], h, "   ")
    assert r.status_code == 422
    assert r.json()["code"] == "empty_message"
    assert chat.client.post(base(w), json={"title": "x" * 201}, headers=h).status_code == 422


def test_unprocessed_upload_is_not_used(chat: Api, ada: tuple[dict[str, str], str]) -> None:
    h, w = ada
    pending = upload(chat, w, h, POLICY, "policy.md").json()["id"]
    conv = create(chat, w, h, document_ids=[pending])
    answer = send(chat, w, conv["id"], h, "Who reviews renewals?").json()["assistant_message"]
    assert answer["answer_status"] == "insufficient_context"
    assert [x["code"] for x in answer["warnings"]] == ["document_not_ready"]
