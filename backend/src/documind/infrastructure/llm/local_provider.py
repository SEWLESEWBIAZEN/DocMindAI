"""Deterministic, offline "LLM" for development and tests. NOT for production.

It answers extractively: it reads the ``<source>`` blocks from the prompt,
quotes the sentences that best match the question with their ``[n]``
citation, and otherwise replies with the insufficient-context sentinel. It
cannot paraphrase or reason; configuration validation rejects it in
production.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from documind.application.ports import (
    ChatMessage,
    LLMDelta,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMUsage,
)
from documind.domain.processing.chunking import split_sentences
from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.prompts import INSUFFICIENT_CONTEXT_SENTINEL

_SOURCE = re.compile(r'<source id="(\d+)"[^>]*>(.*?)</source>', re.DOTALL)
_QUESTION = re.compile(r"<question>(.*?)</question>", re.DOTALL)
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "why",
        "when",
        "where",
        "does",
        "did",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "there",
        "their",
        "them",
        "they",
        "have",
        "has",
        "had",
        "our",
        "your",
        "you",
        "can",
        "could",
        "should",
        "would",
        "about",
        "into",
        "over",
        "under",
        "than",
        "then",
        "also",
        "any",
        "all",
        "not",
        "but",
        "its",
        "per",
        "via",
    ]
)


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


class LocalExtractiveLLM:
    model = "local-extractive-v1"

    def _answer(self, messages: list[ChatMessage]) -> str:
        prompt = "\n".join(m.content for m in messages if m.role == "user")
        question_match = _QUESTION.search(prompt)
        question = _terms(question_match.group(1) if question_match else prompt)
        scored: list[tuple[float, int, str]] = []
        for source_id, body in _SOURCE.findall(prompt):
            for sentence in split_sentences(" ".join(body.split())):
                overlap = len(question & _terms(sentence))
                if question and overlap / len(question) >= 0.34:
                    scored.append((overlap / len(question), int(source_id), sentence))
        if not scored:
            return (
                f"{INSUFFICIENT_CONTEXT_SENTINEL} The provided sources don't contain information "
                "that answers this question."
            )
        best = sorted(scored, key=lambda item: (-item[0], item[1]))[:3]
        return " ".join(f"{sentence} [{source_id}]" for _, source_id, sentence in best)

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        text = self._answer(messages)
        return LLMResult(
            text=text, usage=self._usage(messages, text), model=self.model, finish_reason="stop"
        )

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        text = self._answer(messages)
        for piece in re.findall(r"\S+\s*", text):
            yield LLMDelta(piece)
        yield LLMStreamEnd(
            usage=self._usage(messages, text), model=self.model, finish_reason="stop"
        )

    @staticmethod
    def _usage(messages: list[ChatMessage], text: str) -> LLMUsage:
        return LLMUsage(
            input_tokens=sum(estimate_tokens(m.content) for m in messages),
            output_tokens=estimate_tokens(text),
        )
