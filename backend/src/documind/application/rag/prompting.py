"""Builds the chat messages for a grounded answer from a question, its context and history."""

from __future__ import annotations

import re
from collections.abc import Sequence

from documind.application.ports import ChatMessage
from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.context import neutralize
from documind.domain.rag.models import BuiltContext, Turn
from documind.domain.rag.prompts import PROMPT_VERSION, SYSTEM_PROMPT, USER_TEMPLATE

_MARKERS = re.compile(r"\s*\[\d+(?:\s*[,;]\s*\d+)*\]")


def strip_markers(text: str) -> str:
    """Earlier answers' [n] refer to earlier sources; drop them so they can't be confused."""
    return _MARKERS.sub("", text).strip()


def select_history(history: Sequence[Turn], *, max_turns: int, max_tokens: int) -> list[Turn]:
    """Most recent turns that fit the budget, oldest first."""
    chosen: list[Turn] = []
    used = 0
    for turn in reversed(history[-max_turns:] if max_turns else []):
        cost = estimate_tokens(turn.question) + estimate_tokens(turn.answer)
        if used + cost > max_tokens:
            break
        chosen.append(turn)
        used += cost
    chosen.reverse()
    return chosen


class PromptManager:
    """Single place that turns retrieval output and history into model input.

    Instructions live only in the system message. Earlier turns follow as plain
    user/assistant messages (citation markers removed). The latest message
    carries the sources and the question as data; tags they contain are
    neutralised so nothing can close a ``<source>``/``<question>`` block and
    pose as instructions.
    """

    version = PROMPT_VERSION

    def answer_messages(
        self, question: str, context: BuiltContext, history: Sequence[Turn] = ()
    ) -> list[ChatMessage]:
        messages = [ChatMessage("system", SYSTEM_PROMPT)]
        for turn in history:
            messages.append(ChatMessage("user", neutralize(turn.question)))
            messages.append(ChatMessage("assistant", neutralize(strip_markers(turn.answer))))
        user = USER_TEMPLATE.format(sources=context.text, question=neutralize(question.strip()))
        messages.append(ChatMessage("user", user))
        return messages
