"""Retrieval-augmented answers: retrieve -> build context -> generate -> verify citations."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from documind.application.ports import LLMDelta, LLMProvider, LLMUsage
from documind.application.rag.prompting import PromptManager
from documind.application.rag.retrieval import (
    RetrievalMode,
    RetrievalResult,
    RetrievalService,
    SearchWarning,
)
from documind.domain.errors import ServiceUnavailableError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.domain.processing.errors import ProcessingError
from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.citations import resolve_citations, split_insufficient
from documind.domain.rag.context import build_context
from documind.domain.rag.models import BuiltContext, Citation, ContextSource
from documind.domain.rag.prompts import INSUFFICIENT_CONTEXT_ANSWER, INSUFFICIENT_CONTEXT_SENTINEL

AnswerStatus = Literal["grounded", "insufficient_context"]


@dataclass(frozen=True, slots=True)
class AnswerUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RagAnswer:
    answer: str
    status: AnswerStatus
    citations: tuple[Citation, ...]
    sources: tuple[ContextSource, ...]
    warnings: tuple[SearchWarning, ...]
    model: str | None  # None when no model was called
    prompt_version: str
    retrieval_mode: RetrievalMode
    usage: AnswerUsage
    invalid_citations: int = 0
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AnswerCompleted:
    """Final, verified answer. Its text replaces the streamed deltas, which
    can still contain citation markers that verification removed."""

    result: RagAnswer


AnswerEvent = AnswerDelta | AnswerCompleted


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    max_output_tokens: int
    temperature: float | None
    context_max_tokens: int


@dataclass(slots=True)
class _Prepared:
    question: str
    retrieval: RetrievalResult
    context: BuiltContext
    started: float = field(default_factory=time.monotonic)


def _without_sentinel(text: str) -> str:
    """Drop a leading sentinel but keep trailing whitespace (more deltas follow)."""
    head = text.lstrip()
    if head.startswith(INSUFFICIENT_CONTEXT_SENTINEL):
        return head[len(INSUFFICIENT_CONTEXT_SENTINEL) :].lstrip()
    return text


def _llm_unavailable(exc: ProcessingError) -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "The answer service is temporarily unavailable. Please try again.",
        code=getattr(exc, "code", "llm_unavailable"),
    )


class RAGOrchestrator:
    def __init__(
        self,
        retrieval: RetrievalService,
        llm: LLMProvider,
        options: GenerationOptions,
        prompts: PromptManager | None = None,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._options = options
        self._prompts = prompts or PromptManager()

    async def _prepare(
        self,
        ctx: WorkspaceContext,
        question: str,
        document_ids: Sequence[uuid.UUID] | None,
        collection_ids: Sequence[uuid.UUID] | None,
        top_k: int | None,
    ) -> _Prepared:
        ctx.require(Permission.CONVERSATIONS_USE)
        started = time.monotonic()
        retrieval = await self._retrieval.retrieve(
            ctx,
            question,
            top_k=top_k,
            document_ids=document_ids,
            collection_ids=collection_ids,
        )
        context = build_context(retrieval.chunks, max_tokens=self._options.context_max_tokens)
        return _Prepared(" ".join(question.split()), retrieval, context, started)

    def _finish(
        self,
        prep: _Prepared,
        raw: str,
        *,
        model: str | None,
        usage: LLMUsage | None,
    ) -> RagAnswer:
        insufficient, body = split_insufficient(raw)
        if model is None:  # nothing retrieved; the model was never called
            insufficient, body = True, ""
        resolved = resolve_citations(body, prep.context.sources, prep.question)
        if insufficient:
            text = resolved.text or INSUFFICIENT_CONTEXT_ANSWER
            citations: tuple[Citation, ...] = ()
        else:
            text, citations = resolved.text, resolved.citations
        return RagAnswer(
            answer=text,
            status="insufficient_context" if insufficient else "grounded",
            citations=citations,
            sources=prep.context.sources,
            warnings=tuple(prep.retrieval.warnings),
            model=model,
            prompt_version=self._prompts.version,
            retrieval_mode=prep.retrieval.mode,
            usage=AnswerUsage(
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                embedding_tokens=prep.retrieval.query_tokens,
            ),
            invalid_citations=resolved.invalid_markers,
            latency_ms=round((time.monotonic() - prep.started) * 1000),
        )

    async def answer(
        self,
        ctx: WorkspaceContext,
        question: str,
        *,
        document_ids: Sequence[uuid.UUID] | None = None,
        collection_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
    ) -> RagAnswer:
        prep = await self._prepare(ctx, question, document_ids, collection_ids, top_k)
        if not prep.context.sources:
            return self._finish(prep, "", model=None, usage=None)
        try:
            result = await self._llm.generate(
                self._prompts.answer_messages(prep.question, prep.context),
                max_output_tokens=self._options.max_output_tokens,
                temperature=self._options.temperature,
            )
        except ProcessingError as exc:
            raise _llm_unavailable(exc) from exc
        return self._finish(prep, result.text, model=result.model, usage=result.usage)

    async def stream(
        self,
        ctx: WorkspaceContext,
        question: str,
        *,
        document_ids: Sequence[uuid.UUID] | None = None,
        collection_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """Stream answer text, then one ``AnswerCompleted`` with the verified result.

        Retrieval and authorization happen before the first event, so errors
        there surface as ordinary exceptions. The beginning of the output is
        buffered until it can no longer be the insufficient-context sentinel,
        so the sentinel itself is never shown.
        """
        prep = await self._prepare(ctx, question, document_ids, collection_ids, top_k)
        return self._stream(prep)

    async def _stream(self, prep: _Prepared) -> AsyncIterator[AnswerEvent]:
        if not prep.context.sources:
            final = self._finish(prep, "", model=None, usage=None)
            yield AnswerDelta(final.answer)
            yield AnswerCompleted(final)
            return
        raw: list[str] = []
        pending = ""
        decided = False
        emitted = False
        usage: LLMUsage | None = None
        model = self._llm.model
        try:
            async for event in self._llm.stream(
                self._prompts.answer_messages(prep.question, prep.context),
                max_output_tokens=self._options.max_output_tokens,
                temperature=self._options.temperature,
            ):
                if not isinstance(event, LLMDelta):
                    usage, model = event.usage, event.model
                    continue
                raw.append(event.text)
                if decided:
                    if event.text:
                        emitted = True
                        yield AnswerDelta(event.text)
                    continue
                pending += event.text
                head = pending.lstrip()
                if len(head) < len(INSUFFICIENT_CONTEXT_SENTINEL) and (
                    INSUFFICIENT_CONTEXT_SENTINEL.startswith(head)
                ):
                    continue  # could still become the sentinel
                decided = True
                visible = _without_sentinel(pending)
                if visible:
                    emitted = True
                    yield AnswerDelta(visible)
        except ProcessingError as exc:
            raise _llm_unavailable(exc) from exc
        text = "".join(raw)
        if usage is None:
            usage = LLMUsage(output_tokens=estimate_tokens(text))
        final = self._finish(prep, text, model=model, usage=usage)
        if not emitted:  # e.g. the model replied with only the sentinel
            yield AnswerDelta(final.answer)
        yield AnswerCompleted(final)
