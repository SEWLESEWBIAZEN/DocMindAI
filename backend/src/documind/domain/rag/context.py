"""Builds the model context from retrieved chunks.

* Packs the best chunks into a token budget, skipping near-duplicates.
* Numbers sources 1..n; the answer cites them as [n].
* Neutralises markup in document text and metadata so a document can't close
  its <source> block and smuggle in instructions (prompt injection).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.models import BuiltContext, ContextSource, RetrievedChunk

_TAG = re.compile(r"<(\s*/?\s*)(sources?|question|system|instructions?)\b", re.IGNORECASE)
_WORD = re.compile(r"\w+")
_NEAR_DUPLICATE = 0.8


def neutralize(text: str) -> str:
    """Make prompt-structure tags inert while keeping the text readable."""
    return _TAG.sub(lambda m: "\u2039" + m.group(1) + m.group(2), text)


def _attr(value: str) -> str:
    cleaned = " ".join(neutralize(value).split())
    return (
        cleaned.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def page_label(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    return str(start) if end in (None, start) else f"{start}-{end}"


def _source_block(number: int, chunk: RetrievedChunk) -> str:
    attrs = [f'id="{number}"', f'document="{_attr(chunk.document_title)}"']
    pages = page_label(chunk.page_start, chunk.page_end)
    if pages:
        attrs.append(f'page="{pages}"')
    if chunk.heading_path:
        attrs.append(f'section="{_attr(" > ".join(chunk.heading_path))}"')
    return f"<source {' '.join(attrs)}>\n{neutralize(chunk.text)}\n</source>"


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _is_near_duplicate(candidate: RetrievedChunk, chosen: list[RetrievedChunk]) -> bool:
    words = _words(candidate.text)
    for other in chosen:
        if other.document_id != candidate.document_id:
            continue
        other_words = _words(other.text)
        union = words | other_words
        if union and len(words & other_words) / len(union) >= _NEAR_DUPLICATE:
            return True
    return False


def build_context(chunks: Sequence[RetrievedChunk], *, max_tokens: int) -> BuiltContext:
    """``chunks`` must already be ordered best-first."""
    chosen: list[RetrievedChunk] = []
    blocks: list[str] = []
    tokens = 0
    for chunk in chunks:
        if _is_near_duplicate(chunk, chosen):
            continue
        block = _source_block(len(chosen) + 1, chunk)
        size = estimate_tokens(block)
        if chosen and tokens + size > max_tokens:
            continue  # a smaller, lower-ranked chunk may still fit
        chosen.append(chunk)
        blocks.append(block)
        tokens += size
    return BuiltContext(
        sources=tuple(ContextSource(i + 1, c) for i, c in enumerate(chosen)),
        text="\n\n".join(blocks),
        token_count=tokens,
        dropped=len(chunks) - len(chosen),
    )
