"""Turns figure images found during extraction into text descriptions (ADR 0011).

Runs between extraction and normalization. Each figure block becomes a
paragraph "Figure: <description>", so the figure is searchable, citable and
can be read aloud. Figures that can't be described are dropped; a failure
never fails the document.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from documind.application.ports import ImageDescriber
from documind.domain.processing.errors import ProcessingError
from documind.domain.processing.model import Block, BlockKind, ExtractedDocument

logger = logging.getLogger(__name__)

_CAPTION_PREFIXES = ("figure", "fig.", "fig ", "chart", "graph", "diagram", "image", "exhibit")
MAX_DESCRIPTION_CHARS = 1200


def _caption(blocks: tuple[Block, ...], index: int) -> str | None:
    """A neighbouring paragraph that looks like a caption."""
    for neighbour in (index + 1, index - 1):
        if 0 <= neighbour < len(blocks):
            block = blocks[neighbour]
            text = block.text.strip()
            if block.kind is BlockKind.PARAGRAPH and text.lower().startswith(_CAPTION_PREFIXES):
                return text[:300]
    return None


class FigureDescriber:
    def __init__(self, describer: ImageDescriber | None) -> None:
        self._describer = describer

    async def describe(self, doc: ExtractedDocument) -> tuple[ExtractedDocument, int, int]:
        """Returns the document with figures described, plus (described, failed)."""
        described = failed = 0
        pages = []
        for page in doc.pages:
            blocks: list[Block] = []
            heading: str | None = None
            for index, block in enumerate(page.blocks):
                if block.kind is BlockKind.HEADING:
                    heading = block.text.strip()
                if block.kind is not BlockKind.FIGURE:
                    blocks.append(block)
                    continue
                if self._describer is None or block.image is None:
                    continue
                context_parts = [f"Document: {doc.title}" if doc.title else None]
                if page.page_number is not None:
                    context_parts.append(f"Page {page.page_number}")
                if heading:
                    context_parts.append(f"Section: {heading}")
                caption = _caption(page.blocks, index)
                if caption:
                    context_parts.append(f"Caption: {caption}")
                context = "\n".join(p for p in context_parts if p)
                try:
                    result = await self._describer.describe(
                        block.image, media_type="image/png", context=context
                    )
                except ProcessingError as exc:
                    logger.warning("figure description failed", extra={"code": exc.code})
                    failed += 1
                    continue
                text = " ".join(result.text.split())[:MAX_DESCRIPTION_CHARS]
                if not text:
                    failed += 1
                    continue
                blocks.append(Block(BlockKind.PARAGRAPH, f"Figure: {text}"))
                described += 1
            pages.append(replace(page, blocks=tuple(blocks)))
        warnings = list(doc.warnings)
        if described:
            warnings.append(f"figures_described:count={described}")
        if failed:
            warnings.append(f"figure_descriptions_failed:count={failed}")
        return replace(doc, pages=tuple(pages), warnings=tuple(warnings)), described, failed
