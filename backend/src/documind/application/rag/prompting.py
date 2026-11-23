"""Builds the chat messages for a grounded answer from a question and its context."""

from __future__ import annotations

from documind.application.ports import ChatMessage
from documind.domain.rag.context import neutralize
from documind.domain.rag.models import BuiltContext
from documind.domain.rag.prompts import PROMPT_VERSION, SYSTEM_PROMPT, USER_TEMPLATE


class PromptManager:
    """Single place that turns retrieval output into model input.

    Instructions live only in the system message. The question and sources are
    data in the user message; tags they contain are neutralised so neither can
    close the ``<question>``/``<source>`` blocks and pose as instructions.
    """

    version = PROMPT_VERSION

    def answer_messages(self, question: str, context: BuiltContext) -> list[ChatMessage]:
        user = USER_TEMPLATE.format(sources=context.text, question=neutralize(question.strip()))
        return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]
