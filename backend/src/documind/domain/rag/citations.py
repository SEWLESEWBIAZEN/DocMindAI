"""Turns ``[n]`` markers in a generated answer into verified citations.

Only numbers that refer to a source actually given to the model are kept;
anything else (a hallucinated reference) is removed from the text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from documind.domain.processing.chunking import split_sentences
from documind.domain.rag.models import Citation, ContextSource
from documind.domain.rag.prompts import INSUFFICIENT_CONTEXT_SENTINEL

_MARKER = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_WORD = re.compile(r"\w{3,}")
EXCERPT_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class ResolvedAnswer:
    text: str
    citations: tuple[Citation, ...]
    invalid_markers: int


def split_insufficient(text: str) -> tuple[bool, str]:
    """Detect (and strip) the model's insufficient-context sentinel."""
    stripped = text.lstrip()
    if stripped.startswith(INSUFFICIENT_CONTEXT_SENTINEL):
        return True, stripped[len(INSUFFICIENT_CONTEXT_SENTINEL) :].strip()
    return False, text


def _labels(
    title: str, start: int | None, end: int | None, path: tuple[str, ...]
) -> tuple[str, str]:
    if start is None:
        where = f"section {path[-1]}" if path else ""
        return (f"{title}, {where}" if where else title), (f"{title}, {where}" if where else title)
    if end in (None, start):
        return f"{title}, p. {start}", f"{title}, page {start}"
    return f"{title}, pp. {start}\u2013{end}", f"{title}, pages {start} to {end}"


def _excerpt(chunk_text: str, evidence: str) -> str:
    """The chunk sentence(s) that best support the citing text."""
    wanted = {w.lower() for w in _WORD.findall(evidence)}
    sentences = split_sentences(" ".join(chunk_text.split())) or [chunk_text]
    best = max(sentences, key=lambda s: len(wanted & {w.lower() for w in _WORD.findall(s)}))
    if len(best) > EXCERPT_MAX_CHARS:
        best = best[: EXCERPT_MAX_CHARS - 1].rsplit(" ", 1)[0] + "\u2026"
    return best


def _citing_sentence(text: str, position: int) -> str:
    start = max(text.rfind(". ", 0, position), text.rfind("\n", 0, position)) + 1
    end_candidates = [i for i in (text.find(". ", position), text.find("\n", position)) if i != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def resolve_citations(
    answer: str, sources: Sequence[ContextSource], question: str
) -> ResolvedAnswer:
    by_number = {s.number: s for s in sources}
    order: list[int] = []
    evidence: dict[int, list[str]] = {}
    invalid = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid
        numbers = [int(n) for n in re.split(r"\s*[,;]\s*", match.group(1))]
        kept = []
        for n in numbers:
            if n in by_number:
                kept.append(n)
                if n not in order:
                    order.append(n)
                evidence.setdefault(n, []).append(_citing_sentence(answer, match.start()))
            else:
                invalid += 1
        return "".join(f"[{n}]" for n in kept)

    text = _MARKER.sub(replace, answer)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()

    citations = []
    for n in order:
        chunk = by_number[n].chunk
        label, spoken = _labels(
            chunk.document_title, chunk.page_start, chunk.page_end, chunk.heading_path
        )
        citations.append(
            Citation(
                number=n,
                document_id=chunk.document_id,
                document_name=chunk.document_title,
                chunk_id=chunk.chunk_id,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                heading_path=chunk.heading_path,
                excerpt=_excerpt(chunk.text, " ".join(evidence.get(n, [])) + " " + question),
                label=label,
                spoken_label=spoken,
            )
        )
    return ResolvedAnswer(text=text, citations=tuple(citations), invalid_markers=invalid)
