"""The whole answer path against the real OpenAI API (opt-in).

Run:  OPENAI_API_KEY=... LIVE_OPENAI_CHAT_MODEL=<a chat model your key can use> \
      uv run pytest -m "integration and live" tests/integration/test_openai_end_to_end_live.py -v

Upload a document; real embeddings index it; a question streams a real answer with
verified citations; a follow-up uses the history; usage lands in the ledger. About
2-3k tokens in total.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import SecretStr

from tests.integration.conftest import Api
from tests.integration.test_conversations_api import base, create, parse_sse, send
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_rag_api import POLICY, ingest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set"),
]


@pytest.fixture
def real(make_api: Any) -> Api:
    api: Api = make_api(
        embedding_provider="openai",
        llm_provider="openai",
        openai_api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
        embedding_model=os.environ.get("LIVE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        llm_model=os.environ.get("LIVE_OPENAI_CHAT_MODEL", "gpt-5.4-mini"),
    )
    return api


def test_upload_index_ask_stream_cite_and_meter(real: Api) -> None:
    h = real.signup("live@example.com")
    w = ws(real, h)
    doc = ingest(real, w, h, POLICY, "policy.md")
    chunks = sql(
        real,
        "SELECT count(*) AS n, min(embedding_model) AS model FROM document_chunks "
        "WHERE document_id = :d AND embedding IS NOT NULL",
        d=doc,
    )[0]
    print("indexed chunks:", chunks.n, "with", chunks.model)
    assert chunks.n > 0

    # A real semantic search.
    hits = real.client.post(
        f"/api/v1/workspaces/{w}/search",
        json={"query": "Who has to look at renewals above fifty thousand?"},
        headers=h,
    ).json()
    print("search mode:", hits["mode"], "top:", hits["results"][0]["text"][:80])
    assert hits["results"]

    # A streamed, cited answer.
    conv = create(real, w, h)
    r = send(real, w, conv["id"], h, "Which renewals require Legal review?", sse=True)
    assert r.status_code == 200
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    assert names[0] == "message.created"
    assert names[-1] == "answer.completed", events[-1]
    final = events[-1][1]["message"]
    print("answer:", final["content"][:200])
    print("citations:", [c["label"] for c in final["citations"]], "status:", final["answer_status"])
    print("usage:", final["usage"], "model:", final["model"], "latency_ms:", final["latency_ms"])
    assert final["status"] == "completed"
    assert final["answer_status"] == "grounded"
    assert final["citations"]
    assert final["usage"]["input_tokens"] > 0

    # A follow-up that only makes sense with the history.
    follow = send(real, w, conv["id"], h, "And who approves the smaller ones?").json()
    answer = follow["assistant_message"]
    print("follow-up:", answer["content"][:200], "|", answer["answer_status"])
    assert answer["status"] == "completed"

    # Something the document doesn't say: no made-up answer.
    nothing = send(real, w, conv["id"], h, "What is the CEO's favourite colour?").json()
    print("unanswerable:", nothing["assistant_message"]["answer_status"])
    assert nothing["assistant_message"]["answer_status"] == "insufficient_context"

    # Metered in the usage ledger.
    usage = real.client.get(f"/api/v1/workspaces/{w}/usage", headers=h).json()
    print("usage ledger:", usage["ai"])
    assert usage["ai"]["llm_input_tokens"] > 0
    assert usage["ai"]["questions"] >= 3
    stored = real.client.get(f"{base(w)}/{conv['id']}/messages", headers=h).json()["items"]
    assert len(stored) == 6
