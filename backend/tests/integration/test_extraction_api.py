"""Structured extraction, comparison and deadlines against real services (ADR 0034).

The offline development model does the extracting, so results are predictable.
"""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import date, timedelta
from typing import Any

import pytest

from documind.application.extraction.reminders import recover_extractions, send_reminders
from documind.application.extraction.runner import ExtractionRunner
from documind.container import build_container
from tests.integration.conftest import Api
from tests.integration.test_onboarding_api import load
from tests.integration.test_processing_pipeline import sql, ws

pytestmark = pytest.mark.integration


class Recorder:
    def __init__(self) -> None:
        self.runs: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        self.runs.append((run_id, workspace_id))
        return True


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def xapi(make_api: Any, recorder: Recorder) -> Api:
    api: Api = make_api({"extraction_dispatch": recorder})
    return api


def run(api: Api, run_id: str) -> str:
    async def go() -> str:
        c = build_container(api.settings)
        try:

            async def llm_for(w: uuid.UUID, user: uuid.UUID | None) -> Any:
                return (await c.ai_resolver().resolve(w, user_id=user)).llm

            return await ExtractionRunner(c.session_factory, llm_for, api.settings).run(
                uuid.UUID(run_id)
            )
        finally:
            await c.aclose()

    return asyncio.run(go())


def url(w: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{w}{tail}"


def samples_ready(api: Api) -> tuple[str, dict[str, str], dict[str, str]]:
    h = api.signup("owner@example.com")
    w = ws(api, h)
    body = load(api, w, h)
    docs = api.client.get(url(w, "/documents"), headers=h).json()["items"]
    titles = {d["title"]: d["id"] for d in docs}
    assert len(body["document_ids"]) == 3
    return w, h, titles


def test_obligations_with_sources_csv_and_usage(xapi: Api, recorder: Recorder) -> None:
    w, h, titles = samples_ready(xapi)
    agreement = titles["Sample: Master Services Agreement"]
    templates = {
        t["key"]: t for t in xapi.client.get(url(w, "/extraction-templates"), headers=h).json()
    }
    assert templates["obligations"]["mode"] == "items"
    assert "due_date" in [f["key"] for f in templates["obligations"]["fields"]]

    r = xapi.client.post(
        url(w, "/extractions"),
        json={"template": "obligations", "document_ids": [agreement]},
        headers=h,
    )
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["status"] == "queued"
    assert recorder.runs == [(uuid.UUID(started["id"]), uuid.UUID(w))]
    assert run(xapi, started["id"]) == "completed"
    assert run(xapi, started["id"]) == "skipped"  # a redelivered message does nothing

    detail = xapi.client.get(url(w, f"/extractions/{started['id']}"), headers=h).json()
    assert detail["status"] == "completed"
    assert detail["documents_done"] == 1
    assert detail["documents"] == {agreement: "Sample: Master Services Agreement"}
    texts = [row["values"]["obligation"]["value"] for row in detail["results"]]
    assert any("72 hours" in t for t in texts)
    assert any("30 days" in t for t in texts)
    breach = next(r for r in detail["results"] if "72 hours" in r["values"]["obligation"]["value"])
    cell = breach["values"]["obligation"]
    assert cell["chunk_id"]
    assert "72 hours" in cell["excerpt"]
    assert breach["values"]["party"]["value"] == "Provider"

    exported = xapi.client.get(url(w, f"/extractions/{started['id']}/export.csv"), headers=h)
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exported.text.lstrip("﻿"))))
    assert rows[0][:3] == ["Document", "Obligation", "Responsible party"]
    assert len(rows) == len(detail["results"]) + 1
    # AI usage was metered like answers.
    usage = sql(xapi, "SELECT count(*) AS n FROM usage_records WHERE capability = 'chat'")
    assert usage[0].n >= 1


def test_compare_contract_terms_and_limits(xapi: Api, recorder: Recorder) -> None:
    w, h, titles = samples_ready(xapi)
    collection = xapi.client.get(url(w, "/collections"), headers=h).json()[0]["id"]
    r = xapi.client.post(
        url(w, "/extractions"),
        json={"template": "contract_terms", "collection_id": collection},
        headers=h,
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    assert len(r.json()["document_ids"]) == 3
    assert run(xapi, run_id) == "completed"
    detail = xapi.client.get(url(w, f"/extractions/{run_id}"), headers=h).json()
    # Fields mode: one row per document, so documents can be compared side by side.
    assert sorted(r["document_id"] for r in detail["results"]) == sorted(detail["document_ids"])
    agreement = next(
        r
        for r in detail["results"]
        if r["document_id"] == titles["Sample: Master Services Agreement"]
    )
    assert agreement["values"]["effective_date"]["date"] == "2026-03-01"
    assert "England and Wales" in agreement["values"]["governing_law"]["value"]

    # Custom fields and validation.
    custom = xapi.client.post(
        url(w, "/extractions"),
        json={
            "template": "custom",
            "mode": "fields",
            "fields": [{"label": "Warranty", "description": "Warranty period"}],
            "document_ids": [titles["Sample: Aurora Thermostat FAQ"]],
        },
        headers=h,
    )
    assert custom.status_code == 202, custom.text
    assert custom.json()["fields"][0]["key"] == "warranty"
    assert (
        xapi.client.post(
            url(w, "/extractions"),
            json={
                "template": "custom",
                "fields": [],
                "document_ids": [titles["Sample: Aurora Thermostat FAQ"]],
            },
            headers=h,
        ).json()["code"]
        == "invalid_fields"
    )
    assert (
        xapi.client.post(
            url(w, "/extractions"),
            json={"template": "nope", "document_ids": [titles["Sample: Aurora Thermostat FAQ"]]},
            headers=h,
        ).json()["code"]
        == "invalid_template"
    )
    assert (
        xapi.client.post(
            url(w, "/extractions"), json={"template": "invoice", "document_ids": []}, headers=h
        ).json()["code"]
        == "no_documents"
    )
    assert (
        xapi.client.post(
            url(w, "/extractions"),
            json={"template": "invoice", "document_ids": [str(uuid.uuid4())]},
            headers=h,
        ).status_code
        == 404
    )
    # At most three runs at a time per workspace: custom is queued, add two more.
    for _ in range(2):
        assert (
            xapi.client.post(
                url(w, "/extractions"),
                json={"template": "invoice", "collection_id": collection},
                headers=h,
            ).status_code
            == 202
        )
    busy = xapi.client.post(
        url(w, "/extractions"), json={"template": "invoice", "collection_id": collection}, headers=h
    )
    assert busy.status_code == 409
    assert busy.json()["code"] == "too_many_extractions"

    # Another workspace sees nothing.
    other = xapi.signup("other@example.com")
    assert xapi.client.get(url(w, f"/extractions/{run_id}"), headers=other).status_code in (
        403,
        404,
    )
    ow = ws(xapi, other)
    assert xapi.client.get(url(ow, f"/extractions/{run_id}"), headers=other).status_code == 404
    # Deleting: creator or admin.
    assert xapi.client.delete(url(w, f"/extractions/{run_id}"), headers=h).status_code == 204
    assert xapi.client.get(url(w, f"/extractions/{run_id}"), headers=h).status_code == 404


def test_interrupted_and_lost_runs_are_recovered(xapi: Api, recorder: Recorder) -> None:
    w, h, titles = samples_ready(xapi)
    doc = titles["Sample: Employee Handbook"]
    a = xapi.client.post(
        url(w, "/extractions"), json={"template": "obligations", "document_ids": [doc]}, headers=h
    ).json()
    b = xapi.client.post(
        url(w, "/extractions"), json={"template": "obligations", "document_ids": [doc]}, headers=h
    ).json()
    sql(
        xapi,
        "UPDATE extraction_runs SET status = 'running', "
        "heartbeat_at = now() - interval '1 hour' WHERE id = :i",
        i=uuid.UUID(a["id"]),
    )
    sql(
        xapi,
        "UPDATE extraction_runs SET created_at = now() - interval '10 minutes' WHERE id = :i",
        i=uuid.UUID(b["id"]),
    )
    recorder.runs.clear()

    async def go() -> int:
        c = build_container(xapi.settings)
        try:
            return await recover_extractions(c.session_factory, recorder, stale_seconds=900)
        finally:
            await c.aclose()

    assert asyncio.run(go()) == 2
    failed = xapi.client.get(url(w, f"/extractions/{a['id']}"), headers=h).json()
    assert (failed["status"], failed["error_code"]) == ("failed", "interrupted")
    assert recorder.runs == [(uuid.UUID(b["id"]), uuid.UUID(w))]


def test_deadlines_reminders_and_calendar(xapi: Api) -> None:
    w, h, titles = samples_ready(xapi)
    agreement = titles["Sample: Master Services Agreement"]
    due = date.today() + timedelta(days=10)
    created = xapi.client.post(
        url(w, "/deadlines"),
        json={
            "title": "Send notice of non-renewal",
            "due_date": due.isoformat(),
            "document_id": agreement,
            "source": {"value": "90 days", "page_start": 1, "excerpt": "at least 90 days before"},
        },
        headers=h,
    )
    assert created.status_code == 201, created.text
    d = created.json()
    assert d["remind_days_before"] == [7, 1, 0]
    assert d["document_title"] == "Sample: Master Services Agreement"
    me = xapi.client.get("/api/v1/me", headers=h).json()["id"]
    assert d["assignee_user_ids"] == [me]
    stranger = xapi.signup("stranger@example.com")
    bad = xapi.client.post(
        url(w, "/deadlines"),
        json={
            "title": "x",
            "due_date": due.isoformat(),
            "assignee_user_ids": [xapi.client.get("/api/v1/me", headers=stranger).json()["id"]],
        },
        headers=h,
    )
    assert bad.json()["code"] == "invalid_assignees"

    async def remind(day: date) -> int:
        c = build_container(xapi.settings)
        try:
            return await send_reminders(
                c.session_factory,
                c.session_factory,
                xapi.mail,
                "https://app.example.com",
                today=day,
            )
        finally:
            await c.aclose()

    xapi.mail.sent.clear()
    assert asyncio.run(remind(due - timedelta(days=8))) == 0
    assert asyncio.run(remind(due - timedelta(days=7))) == 1
    assert asyncio.run(remind(due - timedelta(days=7))) == 0  # once
    (mail,) = xapi.mail.to("owner@example.com")
    assert mail.subject == "Due in 7 days: Send notice of non-renewal"
    assert f"/w/{w}/documents/{agreement}" in mail.text
    # Moving the date sends the reminders again for the new date.
    moved = xapi.client.patch(
        url(w, f"/deadlines/{d['id']}"),
        json={"due_date": (due + timedelta(days=1)).isoformat()},
        headers=h,
    ).json()
    assert moved["reminders_sent"] == []
    assert asyncio.run(remind(due)) == 1  # 1 day left: the 7-day reminder is caught up once
    assert asyncio.run(remind(due + timedelta(days=1))) == 1  # on the day

    cal = xapi.client.get(url(w, "/deadlines/calendar.ics"), headers=h)
    assert cal.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Send notice of non-renewal" in cal.text
    done = xapi.client.patch(
        url(w, f"/deadlines/{d['id']}"), json={"status": "done"}, headers=h
    ).json()
    assert done["completed_at"]
    assert xapi.client.get(url(w, "/deadlines"), headers=h).json() == []
    assert len(xapi.client.get(url(w, "/deadlines?status=done"), headers=h).json()) == 1
    # Deleting the document removes its deadlines.
    secret, _ = xapi.enable_mfa(h)
    elevated = xapi.step_up(h, secret)
    assert (
        xapi.client.delete(url(w, f"/documents/{agreement}"), headers=elevated).status_code == 204
    )
    assert xapi.client.get(url(w, "/deadlines?status=all"), headers=h).json() == []


def test_viewers_can_extract_but_not_track(xapi: Api) -> None:
    owner = xapi.signup("owner@example.com")
    org = str(xapi.create_org(owner, "Acme")["id"])
    load(xapi, org, owner)
    viewer = xapi.add_member(org, owner, "vic@example.com", "viewer")
    doc = xapi.client.get(url(org, "/documents"), headers=viewer).json()["items"][0]["id"]
    r = xapi.client.post(
        url(org, "/extractions"),
        json={"template": "obligations", "document_ids": [doc]},
        headers=viewer,
    )
    assert r.status_code == 202, r.text
    assert (
        xapi.client.post(
            url(org, "/deadlines"), json={"title": "x", "due_date": "2030-01-01"}, headers=viewer
        ).status_code
        == 403
    )
    assert (
        xapi.client.delete(url(org, f"/extractions/{r.json()['id']}"), headers=viewer).status_code
        == 204
    )
