"""Turns a follow-up question into a standalone retrieval query.

"What about the second one?" retrieves nothing useful on its own. The
condenser asks the LLM to resolve such references from recent turns and falls
back to a deterministic query (previous question + this one) whenever the
model is unavailable or answers with something that isn't a usable query.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from documind.application.ports import ChatMessage, LLMProvider, LLMUsage
from documind.application.rag.retrieval import MAX_QUERY_CHARS
from documind.domain.processing.errors import ProcessingError
from documind.domain.rag.context import neutralize
from documind.domain.rag.models import Turn
from documind.domain.rag.prompts import (
    CONDENSE_SYSTEM_PROMPT,
    CONDENSE_USER_TEMPLATE,
    INSUFFICIENT_CONTEXT_SENTINEL,
)

logger = logging.getLogger(__name__)

_MAX_CONDENSED_CHARS = 500
_TURNS = 3
_ANSWER_CHARS = 600


@dataclass(frozen=True, slots=True)
class CondensedQuery:
    query: str
    method: str  # "unchanged" | "llm" | "fallback"
    usage: LLMUsage


def fallback_query(history: Sequence[Turn], question: str) -> str:
    if not history:
        return question
    return f"{history[-1].question} {question}"[:MAX_QUERY_CHARS]


class QueryCondenser:
    def __init__(self, llm: LLMProvider, *, enabled: bool, temperature: float | None) -> None:
        self._llm = llm
        self._enabled = enabled
        self._temperature = temperature

    def _messages(self, history: Sequence[Turn], question: str) -> list[ChatMessage]:
        lines: list[str] = []
        for turn in history[-_TURNS:]:
            lines.append(f"User: {neutralize(turn.question)}")
            lines.append(f"Assistant: {neutralize(turn.answer[:_ANSWER_CHARS])}")
        user = CONDENSE_USER_TEMPLATE.format(
            conversation="\n".join(lines), latest=neutralize(question)
        )
        return [ChatMessage("system", CONDENSE_SYSTEM_PROMPT), ChatMessage("user", user)]

    async def condense(self, history: Sequence[Turn], question: str) -> CondensedQuery:
        if not history:
            return CondensedQuery(question, "unchanged", LLMUsage())
        if not self._enabled:
            return CondensedQuery(fallback_query(history, question), "fallback", LLMUsage())
        try:
            result = await self._llm.generate(
                self._messages(history, question),
                max_output_tokens=120,
                temperature=self._temperature,
            )
        except ProcessingError as exc:
            logger.warning("follow-up condensation failed", extra={"error_code": exc.code})
            return CondensedQuery(fallback_query(history, question), "fallback", LLMUsage())
        lines = result.text.strip().splitlines()
        query = " ".join(lines[0].split()).strip("\"'") if lines else ""
        if (
            not query
            or query.startswith(INSUFFICIENT_CONTEXT_SENTINEL)
            or (len(query) > _MAX_CONDENSED_CHARS)
        ):
            return CondensedQuery(fallback_query(history, question), "fallback", result.usage)
        return CondensedQuery(query, "llm", result.usage)
