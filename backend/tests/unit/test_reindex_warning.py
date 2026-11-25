"""The notice shown when some documents are indexed with another embedding model."""

from __future__ import annotations

from documind.application.rag.retrieval import REINDEX_WARNING, reindex_warning


def test_single_document_in_vector_mode_was_not_searched() -> None:
    w = reindex_warning(1, ["MSA"], mode="vector")
    assert w.code == REINDEX_WARNING
    assert w.count == 1
    assert w.message.startswith("1 document was indexed with a different AI model ('MSA')")
    assert "so it was not searched. Re-index it to include it fully." in w.message


def test_hybrid_mode_says_only_exact_words_matched() -> None:
    w = reindex_warning(2, ["A", "B"], mode="hybrid")
    assert "2 documents were indexed" in w.message
    assert "('A' and 'B')" in w.message
    assert "so they were matched only by exact words" in w.message
    assert "Re-index them" in w.message


def test_long_lists_are_shortened() -> None:
    w = reindex_warning(12, ["A", "B", "C"], mode="vector")
    assert "('A', 'B', 'C' and 9 more)" in w.message
