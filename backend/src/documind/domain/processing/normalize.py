"""Text normalization applied to extracted blocks before chunking."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import replace

from documind.domain.processing.model import Block, BlockKind, ExtractedDocument, ExtractedPage

_HYPHEN_BREAK = re.compile(r"(\w)-\n(?=[a-z])")
_SPACES = re.compile("[ \t\u00a0\u2000-\u200b\u202f\u205f\u3000]+")
_DIGITS = re.compile(r"\d+")
# Repeated running headers/footers must appear on at least this share of pages.
_REPEAT_THRESHOLD = 0.6
_MIN_PAGES_FOR_REPEAT_DETECTION = 3


def normalize_text(text: str, *, keep_newlines: bool) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = _HYPHEN_BREAK.sub(r"\1", text)  # "infor-\nmation" -> "information"
    if keep_newlines:
        lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line)
    return _SPACES.sub(" ", text.replace("\n", " ")).strip()


def _signature(text: str) -> str:
    """Compare headers/footers ignoring numbers ("Page 3 of 10" ~ "Page 4 of 10")."""
    return _DIGITS.sub("#", text.strip().lower())


def _repeated_edges(pages: tuple[ExtractedPage, ...]) -> set[str]:
    if len(pages) < _MIN_PAGES_FOR_REPEAT_DETECTION:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        edges = {
            _signature(b.text) for b in (page.blocks[:1] + page.blocks[-1:]) if len(b.text) < 120
        }
        counts.update(edges)
    return {sig for sig, n in counts.items() if n / len(pages) >= _REPEAT_THRESHOLD}


def normalize_document(doc: ExtractedDocument) -> ExtractedDocument:
    """Normalize every block, drop empties and running headers/footers."""
    repeated = _repeated_edges(doc.pages)
    pages = []
    removed = 0
    for page in doc.pages:
        blocks: list[Block] = []
        last = len(page.blocks) - 1
        for i, block in enumerate(page.blocks):
            at_edge = i in (0, last)
            if at_edge and repeated and _signature(block.text) in repeated:
                removed += 1
                continue
            text = normalize_text(block.text, keep_newlines=block.kind is BlockKind.TABLE)
            if text:
                blocks.append(replace(block, text=text))
        pages.append(replace(page, blocks=tuple(blocks)))
    warnings = doc.warnings + ((f"removed_repeated_headers_footers:{removed}",) if removed else ())
    return replace(doc, pages=tuple(pages), warnings=warnings)
