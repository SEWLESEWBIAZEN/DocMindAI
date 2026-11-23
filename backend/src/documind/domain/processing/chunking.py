"""Section- and page-aware chunking with sentence-level overlap.

Rules, in priority order:

1. A heading ends the current chunk; the heading path (e.g. ``Terms > Notice``)
   becomes chunk metadata rather than chunk text.
2. With ``respect_pages`` (default) a chunk never spans pages, so every
   citation points at exactly one page.
3. Paragraphs are kept whole when they fit; larger ones are split at sentence
   boundaries, and only a single sentence longer than the budget is split by
   words. Tables split between rows.
4. Consecutive chunks of the same section and page overlap by whole trailing
   sentences (up to ``overlap_tokens``), never by fragments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from documind.domain.processing.model import BlockKind, ExtractedDocument
from documind.domain.processing.tokens import estimate_tokens

# End of sentence: terminal punctuation (optionally closing quotes/brackets),
# whitespace, then something that looks like the start of a new sentence.
_SENTENCE_BOUNDARY = re.compile(
    '(?<=[.!?\u2026])["\u201d\u2019)\\]]*\\s+(?=["\u201c\u2018(\\[]?[A-Z0-9])'
)


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    max_tokens: int = 500
    overlap_tokens: int = 50
    respect_pages: bool = True

    def __post_init__(self) -> None:
        if self.max_tokens < 20:
            raise ValueError("max_tokens must be at least 20")
        if not 0 <= self.overlap_tokens < self.max_tokens // 2:
            raise ValueError("overlap_tokens must be between 0 and half of max_tokens")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str
    token_count: int
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def _split_words(sentence: str, max_tokens: int) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for word in sentence.split():
        if buf and estimate_tokens(" ".join([*buf, word])) > max_tokens:
            out.append(" ".join(buf))
            buf = []
        buf.append(word)
    if buf:
        out.append(" ".join(buf))
    return out


def _units(text: str, kind: BlockKind, max_tokens: int) -> tuple[list[str], str]:
    """Split a block into the smallest units the chunker may separate, plus the
    joiner that reassembles them (space within prose, newline between rows)."""
    if estimate_tokens(text) <= max_tokens:
        return [text], " "
    if kind is BlockKind.TABLE:
        rows: list[str] = []
        for row in text.split("\n"):
            rows.extend(
                [row] if estimate_tokens(row) <= max_tokens else _split_words(row, max_tokens)
            )
        return rows, "\n"
    units: list[str] = []
    for sentence in split_sentences(text):
        if estimate_tokens(sentence) <= max_tokens:
            units.append(sentence)
        else:
            units.extend(_split_words(sentence, max_tokens))
    return units, " "


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    """Trailing whole sentences totalling at most ``overlap_tokens``."""
    if overlap_tokens <= 0:
        return ""
    tail: list[str] = []
    for sentence in reversed(split_sentences(text.replace("\n\n", " "))):
        candidate = " ".join([sentence, *tail])
        if estimate_tokens(candidate) > overlap_tokens:
            break
        tail.insert(0, sentence)
    return " ".join(tail)


class _Chunker:
    def __init__(self, options: ChunkingOptions) -> None:
        self.o = options
        self.chunks: list[Chunk] = []
        self.text = ""
        self.page_start: int | None = None
        self.page_end: int | None = None
        self.path: tuple[str, ...] = ()

    def flush(self, *, carry_overlap: bool) -> None:
        if not self.text:
            return
        self.chunks.append(
            Chunk(
                index=len(self.chunks),
                text=self.text,
                token_count=estimate_tokens(self.text),
                page_start=self.page_start,
                page_end=self.page_end,
                heading_path=self.path,
            )
        )
        tail = _overlap_tail(self.text, self.o.overlap_tokens) if carry_overlap else ""
        self.text = tail
        self.page_start = self.page_end if tail else None

    def add(self, unit: str, joiner: str, page: int | None, path: tuple[str, ...]) -> None:
        if self.text and estimate_tokens(self.text + joiner + unit) > self.o.max_tokens:
            self.flush(carry_overlap=True)
            if self.text and estimate_tokens(self.text + joiner + unit) > self.o.max_tokens:
                self.text, self.page_start = "", None  # the overlap doesn't fit
        if not self.text:
            self.page_start = page
            self.path = path
            self.text = unit
        else:
            self.text = self.text + joiner + unit
        self.page_end = page


def chunk_document(doc: ExtractedDocument, options: ChunkingOptions | None = None) -> list[Chunk]:
    opts = options or ChunkingOptions()
    state = _Chunker(opts)
    headings: list[tuple[int, str]] = []
    for page in doc.pages:
        if opts.respect_pages and state.text and page.page_number != state.page_end:
            state.flush(carry_overlap=False)
        for block in page.blocks:
            if block.kind is BlockKind.HEADING:
                state.flush(carry_overlap=False)
                level = block.level or 1
                headings = [h for h in headings if h[0] < level] + [(level, block.text)]
                continue
            path = tuple(text for _, text in headings)
            units, joiner = _units(block.text, block.kind, opts.max_tokens)
            for i, unit in enumerate(units):
                # A new block is a new paragraph; units of one block keep its joiner.
                state.add(unit, "\n\n" if i == 0 else joiner, page.page_number, path)
    state.flush(carry_overlap=False)
    return state.chunks


def embedding_input(title: str | None, heading_path: tuple[str, ...], text: str) -> str:
    """Text sent to the embedding model: chunk plus its document and section context."""
    context = [part for part in (title, " > ".join(heading_path)) if part]
    return ("\n".join(context) + "\n\n" + text) if context else text
