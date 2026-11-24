"""Usage ledger, plans and quotas against the real stack (ADR 0020)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from documind.application.billing.service import BillingService
from documind.application.processing.orchestrator import JobOutcome, ProcessingOrchestrator
from documind.container import build_container, processing_ai
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import sql, ws

pytestmark = pytest.mark.integration

POLICY = b"""# Vendor Policy

Vendor renewals above fifty thousand dollars require Legal review before signature.
Either party may terminate the agreement with ninety days written notice.
"""


def run_metered_job(api: Api, job_id: uuid.UUID) -> JobOutcome:
    """Run a job the way the worker does: per-workspace, metered AI."""

    async def go() -> JobOutcome:
        c = build_container(api.settings)
        try:
            return await ProcessingOrchestrator(
                c.session_factory,
                api.settings,
                c.storage,
                c.extractor,
                worker="test",
                ai=processing_ai(c),
            ).run(job_id)
        finally:
            await c.aclose()

    return asyncio.run(go())


def assign(api: Api, workspace: str, plan: str, **overrides: Any) -> None:
    async def go() -> None:
        c = build_container(api.settings)
        try:
            async with c.session_factory() as session:
                await BillingService(session).assign(
                    uuid.UUID(workspace), plan, overrides=overrides, operator="test"
                )
        finally:
            await c.aclose()

    asyncio.run(go())


def ready_document(api: Api, h: dict[str, str]) -> str:
    w = ws(api, h)
    upload(api, w, h, POLICY, "policy.md")
    assert run_metered_job(api, api.dispatcher.dispatched[-1]).status == "succeeded"
    return w


def test_processing_and_answers_are_metered_to_the_platform(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ready_document(api, h)
    answer = api.client.post(
        f"/api/v1/workspaces/{w}/ask", json={"question": "How much notice is needed?"}, headers=h
    )
    assert answer.status_code == 200

    rows = sql(
        api,
        "SELECT capability, billed_to, input_tokens, units, user_id, source_type "
        "FROM usage_records "
        "WHERE workspace_id = :w ORDER BY id",
        w=uuid.UUID(w),
    )
    caps = [(r.capability, r.billed_to) for r in rows]
    assert ("embedding", "platform") in caps  # indexing (worker) and the query (API)
    assert ("chat", "platform") in caps
    indexing = next(r for r in rows if r.source_type == "processing_job")
    assert indexing.user_id is None  # background work has no user
    assert indexing.input_tokens > 0
    chat = next(r for r in rows if r.capability == "chat")
    assert chat.user_id is not None

    plan = api.client.get(f"/api/v1/workspaces/{w}/plan", headers=h).json()
    assert plan["plan_key"] == "free"
    assert plan["entitlements"]["byok"] is True
    quotas = {q["metric"]: q for q in plan["quotas"]}
    assert quotas["chat_tokens"]["used"] > 0
    assert quotas["max_documents"]["used"] == 1
    assert not quotas["chat_tokens"]["exceeded"]
    assert {(u["capability"], u["billed_to"]) for u in plan["usage"]} >= {
        ("embedding", "platform"),
        ("chat", "platform"),
    }


def test_chat_quota_blocks_answers_with_402(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ready_document(api, h)
    assign(api, w, "free", chat_tokens=1)
    ask = {"question": "How much notice is needed?"}
    first = api.client.post(f"/api/v1/workspaces/{w}/ask", json=ask, headers=h)
    assert first.status_code == 200  # nothing used yet: admitted
    second = api.client.post(f"/api/v1/workspaces/{w}/ask", json=ask, headers=h)
    assert second.status_code == 402
    body = second.json()
    assert body["code"] == "quota_exceeded"
    assert body["quota"]["metric"] == "chat_tokens"
    assert body["quota"]["limit"] == 1
    assert body["quota"]["resets_at"]
    # Conversations and summaries are covered by the same admission check.
    conv = api.client.post(f"/api/v1/workspaces/{w}/conversations", json={}, headers=h).json()
    sent = api.client.post(
        f"/api/v1/workspaces/{w}/conversations/{conv['id']}/messages",
        json={"content": "How much notice?"},
        headers=h,
    )
    assert sent.status_code == 402
    # Search doesn't use chat tokens and still works.
    hits = api.client.post(f"/api/v1/workspaces/{w}/search", json={"query": "notice"}, headers=h)
    assert hits.status_code == 200
    # Plan changes are audited.
    actions = [
        r.action
        for r in sql(api, "SELECT action FROM audit_logs WHERE workspace_id = :w", w=uuid.UUID(w))
    ]
    assert "workspace.plan_assigned" in actions


def test_document_and_storage_limits(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ws(api, h)
    assign(api, w, "free", max_documents=1)
    assert upload(api, w, h, POLICY, "one.md").status_code == 201
    blocked = upload(api, w, h, POLICY, "two.md")
    assert blocked.status_code == 402
    assert blocked.json()["quota"]["metric"] == "max_documents"

    assign(api, w, "free", storage_bytes=len(POLICY) + 10)
    too_big = upload(api, w, h, POLICY, "three.md")
    assert too_big.status_code == 402
    assert too_big.json()["quota"]["metric"] == "storage_bytes"


def test_member_limit_counts_pending_invitations(api: Api) -> None:
    h = api.signup("owner@example.com")
    w = str(api.create_org(h, "Acme")["id"])
    assign(api, w, "free", max_members=2)
    invite = {"email": "one@example.com", "role": "member"}
    assert (
        api.client.post(f"/api/v1/workspaces/{w}/invitations", json=invite, headers=h).status_code
        == 201
    )
    second = api.client.post(
        f"/api/v1/workspaces/{w}/invitations",
        json={"email": "two@example.com", "role": "member"},
        headers=h,
    )
    assert second.status_code == 402
    assert second.json()["quota"]["metric"] == "max_members"


def test_plan_requires_usage_read(api: Api) -> None:
    owner = api.signup("owner@example.com")
    w = ws(api, owner)
    stranger = api.signup("eve@example.com")
    assert api.client.get(f"/api/v1/workspaces/{w}/plan", headers=stranger).status_code == 404
    assert api.client.get(f"/api/v1/workspaces/{w}/plan", headers=owner).status_code == 200


def test_usage_records_hold_no_document_content(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ready_document(api, h)
    api.client.post(f"/api/v1/workspaces/{w}/ask", json={"question": "notice?"}, headers=h)
    dump = " ".join(
        str(tuple(r))
        for r in sql(api, "SELECT * FROM usage_records WHERE workspace_id = :w", w=uuid.UUID(w))
    )
    assert "ninety" not in dump
    assert "Vendor" not in dump


def test_server_speech_is_metered_and_capped_by_audio_quota(make_api: Any) -> None:
    from tests.integration.test_voice_api import FakeSpeech, transcribe

    speech = FakeSpeech()
    api: Api = make_api({"speech_to_text": speech, "text_to_speech": speech})
    h = api.signup("ada@example.com")
    w = ws(api, h)
    assert transcribe(api, w, h, data=b"\x1a\x45\xdf\xa3" + b"0" * 7996).status_code == 200
    spoken = api.client.post(
        f"/api/v1/workspaces/{w}/voice/speech", json={"text": "x" * 300}, headers=h
    )
    assert spoken.status_code == 200
    rows = sql(
        api,
        "SELECT capability, audio_seconds, characters, details FROM usage_records "
        "WHERE workspace_id = :w ORDER BY id",
        w=uuid.UUID(w),
    )
    by_cap = {r.capability: r for r in rows}
    assert by_cap["speech_to_text"].audio_seconds == 2.0  # estimated from the recording size
    assert by_cap["speech_to_text"].details == {"estimated": True}
    assert by_cap["text_to_speech"].characters == 300
    assert by_cap["text_to_speech"].audio_seconds == 20.0

    assign(api, w, "free", audio_seconds=10)
    blocked = transcribe(api, w, h)
    assert blocked.status_code == 402
    assert blocked.json()["quota"]["metric"] == "audio_seconds"
