"""Figure descriptions through the real processing pipeline (ADR 0011)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from documind.application.ports import ImageDescription
from documind.application.processing.embedding_service import EmbeddingService
from documind.application.processing.figures import FigureDescriber
from documind.application.processing.orchestrator import JobOutcome, ProcessingOrchestrator
from documind.container import build_container
from documind.infrastructure.database.repositories.processing import SessionEmbeddingCache
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import sql, ws
from tests.unit.test_figures import chart_pdf

pytestmark = pytest.mark.integration


class ChartDescriber:
    model = "fake-vision"

    def __init__(self) -> None:
        self.contexts: list[str] = []

    async def describe(self, image: bytes, *, media_type: str, context: str) -> ImageDescription:
        self.contexts.append(context)
        assert image.startswith(b"\x89PNG")
        return ImageDescription(
            "A bar chart comparing revenue by region; "
            "the Northwind region is highest at 4.2 million."
        )


def run_with_figures(api: Api, job_id: uuid.UUID, describer: Any) -> JobOutcome:
    async def go() -> JobOutcome:
        c = build_container(api.settings)
        try:
            return await ProcessingOrchestrator(
                c.session_factory,
                api.settings,
                c.storage,
                c.extractor,
                EmbeddingService(c.embedding_provider, SessionEmbeddingCache(c.session_factory)),
                worker="test",
                figures=FigureDescriber(describer),
            ).run(job_id)
        finally:
            await c.aclose()

    return asyncio.run(go())


def test_figures_are_described_searchable_and_citable(make_api: Any) -> None:
    api: Api = make_api(figure_descriptions_enabled=True)
    h = api.signup("ada@example.com")
    w = ws(api, h)
    doc = upload(api, w, h, chart_pdf(1), "results.pdf").json()
    describer = ChartDescriber()
    assert run_with_figures(api, api.dispatcher.dispatched[-1], describer).status == "succeeded"
    assert len(describer.contexts) == 1
    assert describer.contexts[0] == (
        "Page 1\nSection: Quarterly results\nCaption: Figure 1: Revenue by region"
    )

    chunks = sql(
        api, "SELECT text FROM document_chunks WHERE document_id = :id", id=uuid.UUID(doc["id"])
    )
    assert any("Figure: A bar chart comparing revenue by region" in row.text for row in chunks)
    detail = api.client.get(
        f"/api/v1/workspaces/{w}/documents/{doc['id']}/processing", headers=h
    ).json()
    assert "figures_described:count=1" in detail["extraction_warnings"]

    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "Northwind region revenue"}, headers=h
    ).json()
    assert "Northwind" in hits["results"][0]["text"]
    answer = api.client.post(
        f"/api/v1/workspaces/{w}/ask",
        json={"question": "Which region has the highest revenue in the chart?"},
        headers=h,
    ).json()
    assert answer["status"] == "grounded"
    assert any("Northwind" in c["excerpt"] for c in answer["citations"])


def test_without_descriptions_figures_add_nothing(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ws(api, h)
    doc = upload(api, w, h, chart_pdf(1), "results.pdf").json()
    assert run_with_figures(api, api.dispatcher.dispatched[-1], None).status == "succeeded"
    chunks = sql(
        api, "SELECT text FROM document_chunks WHERE document_id = :id", id=uuid.UUID(doc["id"])
    )
    assert not any("Figure:" in row.text for row in chunks)
