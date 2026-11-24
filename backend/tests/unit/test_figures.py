import base64
import json
from typing import Any

import httpx2
import pymupdf
import pytest

from documind.application.ports import ImageDescription
from documind.application.processing.figures import FigureDescriber
from documind.domain.processing.errors import TransientProcessingError
from documind.domain.processing.model import Block, BlockKind, ExtractedDocument, ExtractedPage
from documind.infrastructure.llm.vision import (
    FIGURE_SYSTEM_PROMPT,
    LocalImageDescriber,
    OpenAIImageDescriber,
    png_size,
)
from documind.infrastructure.processing.extractors import (
    ExtractionLimits,
    FigureOptions,
    OcrOptions,
    PdfExtractor,
)


class Fake:
    model = "fake"

    def __init__(self, fail_on: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def describe(self, image: bytes, *, media_type: str, context: str) -> ImageDescription:
        self.calls.append(context)
        if self.fail_on == len(self.calls):
            raise TransientProcessingError("down")
        return ImageDescription(f"A bar chart of revenue, call {len(self.calls)}.")


def doc_with_figures(n: int) -> ExtractedDocument:
    blocks: list[Block] = [Block(BlockKind.HEADING, "Results", 1)]
    for i in range(n):
        blocks.append(Block(BlockKind.FIGURE, "", image=b"png"))
        blocks.append(Block(BlockKind.PARAGRAPH, f"Figure {i + 1}: Revenue by region"))
    return ExtractedDocument("Annual report", (ExtractedPage(1, 3, tuple(blocks)),))


async def test_figures_become_described_paragraphs_with_context() -> None:
    fake = Fake()
    doc, described, failed = await FigureDescriber(fake).describe(doc_with_figures(2))
    texts = [b.text for b in doc.pages[0].blocks]
    assert texts[1] == "Figure: A bar chart of revenue, call 1."
    assert all(b.kind is not BlockKind.FIGURE and b.image is None for b in doc.pages[0].blocks)
    assert (described, failed) == (2, 0)
    assert (
        fake.calls[0]
        == "Document: Annual report\nPage 3\nSection: Results\nCaption: Figure 1: Revenue by region"
    )
    assert "figures_described:count=2" in doc.warnings


async def test_failures_drop_the_figure_but_not_the_document() -> None:
    doc, described, failed = await FigureDescriber(Fake(fail_on=1)).describe(doc_with_figures(2))
    assert (described, failed) == (1, 1)
    assert sum(b.text.startswith("Figure: ") for b in doc.pages[0].blocks) == 1
    assert "figure_descriptions_failed:count=1" in doc.warnings


async def test_without_a_describer_figures_are_removed() -> None:
    doc, described, _ = await FigureDescriber(None).describe(doc_with_figures(1))
    assert described == 0
    assert [b.kind for b in doc.pages[0].blocks] == [BlockKind.HEADING, BlockKind.PARAGRAPH]


def chart_pdf(figures: int = 1, full_page_scan: bool = False) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Quarterly results", fontsize=18)
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), 0)
    image.set_rect(image.irect, (40, 120, 200))
    y = 100.0
    for i in range(figures):
        page.insert_image(pymupdf.Rect(72, y, 372, y + 150), pixmap=image)
        page.insert_text((72, y + 170), f"Figure {i + 1}: Revenue by region", fontsize=10)
        y += 200
    page.insert_image(pymupdf.Rect(500, 40, 520, 60), pixmap=image)  # a tiny icon
    if full_page_scan:
        scan = pdf.new_page()
        scan.insert_image(scan.rect, pixmap=image, keep_proportion=False)
        scan.insert_text((72, 72), "Scanned text " * 5, fontsize=11)
    return bytes(pdf.tobytes())


def extractor(enabled: bool = True, max_per_document: int = 20) -> PdfExtractor:
    return PdfExtractor(
        ExtractionLimits(),
        OcrOptions(enabled=False),
        FigureOptions(enabled=enabled, max_per_document=max_per_document),
    )


def test_pdf_figures_are_captured_in_reading_order() -> None:
    doc = extractor().extract(chart_pdf(2, full_page_scan=True))
    kinds = [b.kind for b in doc.pages[0].blocks]
    assert kinds.count(BlockKind.FIGURE) == 2  # the 20pt icon is ignored
    first = kinds.index(BlockKind.FIGURE)
    assert doc.pages[0].blocks[first - 1].text == "Quarterly results"
    assert doc.pages[0].blocks[first + 1].text.startswith("Figure 1")
    image = doc.pages[0].blocks[first].image
    assert image is not None
    assert png_size(image) is not None
    assert all(b.kind is not BlockKind.FIGURE for b in doc.pages[1].blocks)  # full-page image


def test_figure_capture_is_off_by_default_and_capped() -> None:
    assert BlockKind.FIGURE not in [
        b.kind for b in extractor(enabled=False).extract(chart_pdf()).pages[0].blocks
    ]
    capped = extractor(max_per_document=1).extract(chart_pdf(2))
    assert [b.kind for b in capped.pages[0].blocks].count(BlockKind.FIGURE) == 1
    assert "figures_not_described:count=1" in capped.warnings


async def test_openai_describer_sends_the_image() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5.4-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": " A pie chart. "},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 12, "total_tokens": 912},
            },
        )

    describer = OpenAIImageDescriber(
        api_key="sk-test",
        model="gpt-5.4-mini",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    result = await describer.describe(b"PNGDATA", media_type="image/png", context="Page 2")
    assert result.text == "A pie chart."
    assert result.usage.input_tokens == 900
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": FIGURE_SYSTEM_PROMPT}
    parts = body["messages"][1]["content"]
    assert parts[0]["text"].endswith("Page 2")
    assert (
        parts[1]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(b"PNGDATA").decode()
    )


async def test_openai_describer_classifies_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": {"message": "x"}})

    describer = OpenAIImageDescriber(
        api_key="sk-test",
        model="m",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(TransientProcessingError):
        await describer.describe(b"x", media_type="image/png", context="")


async def test_local_describer_never_invents_content() -> None:
    png = extractor().extract(chart_pdf()).pages[0].blocks[1].image or b""
    result = await LocalImageDescriber().describe(
        png, media_type="image/png", context="Caption: Figure 1: Sales"
    )
    assert 'captioned "Figure 1: Sales"' in result.text
    assert "pixels" in result.text
    assert "can't see images" in result.text
