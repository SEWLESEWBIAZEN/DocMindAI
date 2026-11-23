"""What an extractor produces: pages of typed blocks, plus metadata and warnings."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    text: str
    # Heading depth (1 = top level); None for non-headings.
    level: int | None = None


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    # 1-based order within the document.
    position: int
    # Printed page number for paginated formats (PDF; DOCX when Word recorded
    # page breaks). None when the format has no pages (TXT, Markdown).
    page_number: int | None
    blocks: tuple[Block, ...]
    ocr: bool = False


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    title: str | None
    pages: tuple[ExtractedPage, ...]
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def page_count(self) -> int | None:
        numbered = [p.page_number for p in self.pages if p.page_number is not None]
        return max(numbered) if numbered else None

    @property
    def ocr_pages(self) -> list[int]:
        return [p.page_number or p.position for p in self.pages if p.ocr]

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for p in self.pages for b in p.blocks)
