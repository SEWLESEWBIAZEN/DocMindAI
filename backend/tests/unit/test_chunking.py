import itertools

import pytest

from documind.domain.processing.chunking import (
    ChunkingOptions,
    chunk_document,
    embedding_input,
    split_sentences,
)
from documind.domain.processing.model import Block, BlockKind, ExtractedDocument, ExtractedPage
from documind.domain.processing.normalize import normalize_document, normalize_text
from documind.domain.processing.tokens import estimate_tokens

H = BlockKind.HEADING
P = BlockKind.PARAGRAPH
T = BlockKind.TABLE


def doc(*pages: list[Block], numbered: bool = True) -> ExtractedDocument:
    return ExtractedDocument(
        title="Doc",
        pages=tuple(
            ExtractedPage(
                position=i + 1, page_number=(i + 1) if numbered else None, blocks=tuple(blocks)
            )
            for i, blocks in enumerate(pages)
        ),
    )


def para(text: str) -> Block:
    return Block(P, text)


def sentence(n: int, words: int = 12) -> str:
    return f"Sentence {n} " + " ".join(f"word{n}x{i}" for i in range(words)) + "."


class TestNormalize:
    def test_text_cleanup(self) -> None:
        raw = "Infor-\nmation  is\u00a0here\x07 and\nthere. Jean-\nPaul \ufb01ne"
        assert (
            normalize_text(raw, keep_newlines=False)
            == "Information is here and there. Jean- Paul fine"
        )

    def test_table_keeps_rows(self) -> None:
        assert normalize_text("a |  b\n\n c | d ", keep_newlines=True) == "a | b\nc | d"

    def test_repeated_headers_and_footers_removed(self) -> None:
        pages = [
            [para("ACME CONFIDENTIAL"), para(f"Body text of page {i}."), para(f"Page {i} of 4")]
            for i in range(1, 5)
        ]
        result = normalize_document(doc(*pages))
        texts = [b.text for p in result.pages for b in p.blocks]
        assert texts == [f"Body text of page {i}." for i in range(1, 5)]
        assert "removed_repeated_headers_footers:8" in result.warnings

    def test_short_documents_keep_edges(self) -> None:
        result = normalize_document(doc([para("Title")], [para("Title")]))
        assert sum(len(p.blocks) for p in result.pages) == 2

    def test_empty_blocks_dropped(self) -> None:
        result = normalize_document(doc([para("  \x00 "), para("Kept")]))
        assert [b.text for b in result.pages[0].blocks] == ["Kept"]


class TestSentences:
    def test_split(self) -> None:
        text = 'First one. "Second" here! Third? 4th item. e.g. lowercase continues.'
        assert split_sentences(text) == [
            "First one.",
            '"Second" here!',
            "Third?",
            "4th item. e.g. lowercase continues.",
        ]


class TestChunking:
    def test_small_document_is_one_chunk_with_page(self) -> None:
        chunks = chunk_document(doc([para("Hello world."), para("Second paragraph.")]))
        assert len(chunks) == 1
        assert chunks[0].text == "Hello world.\n\nSecond paragraph."
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)

    def test_headings_start_new_chunks_and_set_path(self) -> None:
        chunks = chunk_document(
            doc(
                [
                    Block(H, "Terms", 1),
                    para("Intro text."),
                    Block(H, "Notice", 2),
                    para("Ninety days."),
                    Block(H, "Fees", 1),
                    para("Fees apply."),
                ]
            )
        )
        assert [(c.text, c.heading_path) for c in chunks] == [
            ("Intro text.", ("Terms",)),
            ("Ninety days.", ("Terms", "Notice")),
            ("Fees apply.", ("Fees",)),
        ]

    def test_page_boundaries_respected(self) -> None:
        chunks = chunk_document(doc([para("Page one text.")], [para("Page two text.")]))
        assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]

    def test_pages_can_be_merged_when_allowed(self) -> None:
        options = ChunkingOptions(max_tokens=200, overlap_tokens=0, respect_pages=False)
        chunks = chunk_document(doc([para("Page one text.")], [para("Page two text.")]), options)
        assert [(c.page_start, c.page_end) for c in chunks] == [(1, 2)]

    def test_budget_respected_and_sentences_not_split(self) -> None:
        body = " ".join(sentence(i) for i in range(40))
        options = ChunkingOptions(max_tokens=80, overlap_tokens=0)
        chunks = chunk_document(doc([para(body)]), options)
        assert len(chunks) > 3
        for c in chunks:
            assert c.token_count <= 80
            assert c.text.endswith(".")
            assert c.text.startswith("Sentence ")
        rejoined = " ".join(c.text for c in chunks)
        assert rejoined == body

    def test_overlap_uses_whole_trailing_sentences(self) -> None:
        body = " ".join(sentence(i, words=6) for i in range(30))
        options = ChunkingOptions(max_tokens=80, overlap_tokens=20)
        chunks = chunk_document(doc([para(body)]), options)
        for previous, current in itertools.pairwise(chunks):
            last_sentence = split_sentences(previous.text)[-1]
            assert current.text.startswith(last_sentence)
            assert current.token_count <= 80

    def test_no_overlap_across_sections_or_pages(self) -> None:
        options = ChunkingOptions(max_tokens=80, overlap_tokens=20)
        chunks = chunk_document(
            doc(
                [Block(H, "A", 1), para(sentence(1))],
                [para(sentence(2))],
                [Block(H, "B", 1), para(sentence(3))],
            ),
            options,
        )
        assert [c.text for c in chunks] == [sentence(1), sentence(2), sentence(3)]

    def test_giant_sentence_split_by_words(self) -> None:
        giant = " ".join(f"w{i}" for i in range(400))  # no sentence boundary
        chunks = chunk_document(
            doc([para(giant)]), ChunkingOptions(max_tokens=50, overlap_tokens=0)
        )
        assert all(c.token_count <= 50 for c in chunks)
        assert " ".join(c.text for c in chunks) == giant

    def test_tables_split_between_rows(self) -> None:
        table = "\n".join(f"row {i} | value {i} | note {i}" for i in range(60))
        chunks = chunk_document(
            doc([Block(T, table)]), ChunkingOptions(max_tokens=60, overlap_tokens=0)
        )
        assert len(chunks) > 1
        assert all(line.startswith("row ") for c in chunks for line in c.text.split("\n"))

    def test_non_paginated_documents(self) -> None:
        chunks = chunk_document(doc([para("Text only.")], numbered=False))
        assert (chunks[0].page_start, chunks[0].page_end) == (None, None)

    def test_indexes_are_sequential(self) -> None:
        body = " ".join(sentence(i) for i in range(30))
        chunks = chunk_document(
            doc([para(body)], [para(body)]), ChunkingOptions(max_tokens=60, overlap_tokens=10)
        )
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_options_validation(self) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            ChunkingOptions(max_tokens=5)
        with pytest.raises(ValueError, match="overlap"):
            ChunkingOptions(max_tokens=100, overlap_tokens=60)


def test_embedding_input_adds_context() -> None:
    assert (
        embedding_input("MSA", ("Terms", "Notice"), "Ninety days.")
        == "MSA\nTerms > Notice\n\nNinety days."
    )
    assert embedding_input(None, (), "Plain.") == "Plain."


def test_token_estimate_is_conservative() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("word " * 100) >= 125
