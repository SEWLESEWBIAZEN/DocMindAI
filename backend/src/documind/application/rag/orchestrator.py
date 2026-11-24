"""Retrieval-augmented answers: (condense) -> retrieve -> build context -> generate -> verify.

Two-step API so callers can do work between retrieval and generation (the
conversation service persists messages there):

- ``prepare()`` authorizes, condenses follow-ups, retrieves and builds the
  context. Errors here are ordinary exceptions raised before anything streams.
- ``generate()`` / ``stream_prepared()`` call the model and verify citations.

``answer()`` and ``stream()`` combine the two for one-shot questions.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from documind.application.ports import ChatMessage, LLMDelta, LLMProvider, LLMUsage
from documind.application.rag.condense import QueryCondenser
from documind.application.rag.prompting import PromptManager, select_history
from documind.application.rag.retrieval import (
    RetrievalMode,
    RetrievalResult,
    RetrievalService,
    SearchWarning,
)
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.domain.processing.errors import ProcessingError, ProviderAccessError
from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.citations import resolve_citations, split_insufficient
from documind.domain.rag.context import build_context
from documind.domain.rag.models import BuiltContext, Citation, ContextSource, Turn
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
    retrieval_query: str = ""
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
    history_max_turns: int = 6
    history_max_tokens: int = 2000


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    question: str
    document_ids: Sequence[uuid.UUID] | None = None
    collection_ids: Sequence[uuid.UUID] | None = None
    top_k: int | None = None
    history: Sequence[Turn] = ()


@dataclass(slots=True)
class PreparedAnswer:
    question: str
    retrieval_query: str
    retrieval: RetrievalResult
    context: BuiltContext
    history: list[Turn]
    condense_usage: LLMUsage = field(default_factory=LLMUsage)
    started: float = field(default_factory=time.monotonic)


def _without_sentinel(text: str) -> str:
    """Drop a leading sentinel but keep trailing whitespace (more deltas follow)."""
    head = text.lstrip()
    if head.startswith(INSUFFICIENT_CONTEXT_SENTINEL):
        return head[len(INSUFFICIENT_CONTEXT_SENTINEL) :].lstrip()
    return text


def _llm_unavailable(exc: ProcessingError) -> ServiceUnavailableError:
    if isinstance(exc, ProviderAccessError):
        return ServiceUnavailableError(exc.message, code=exc.code)
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
        condenser: QueryCondenser | None = None,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._options = options
        self._prompts = prompts or PromptManager()
        self._condenser = condenser or QueryCondenser(
            llm, enabled=True, temperature=options.temperature
        )

    @property
    def prompt_version(self) -> str:
        return self._prompts.version

    async def prepare(self, ctx: WorkspaceContext, request: AnswerRequest) -> PreparedAnswer:
        ctx.require(Permission.CONVERSATIONS_USE)
        started = time.monotonic()
        question = " ".join(request.question.split())
        history = select_history(
            request.history,
            max_turns=self._options.history_max_turns,
            max_tokens=self._options.history_max_tokens,
        )
        condensed = await self._condenser.condense(history, question) if question else None
        query = condensed.query if condensed else question
        retrieval = await self._retrieval.retrieve(
            ctx,
            query,
            top_k=request.top_k,
            document_ids=request.document_ids,
            collection_ids=request.collection_ids,
        )
        context = build_context(retrieval.chunks, max_tokens=self._options.context_max_tokens)
        return PreparedAnswer(
            question=question,
            retrieval_query=query,
            retrieval=retrieval,
            context=context,
            history=history,
            condense_usage=condensed.usage if condensed else LLMUsage(),
            started=started,
        )

    def _finish(
        self,
        prep: PreparedAnswer,
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
                input_tokens=(usage.input_tokens if usage else 0)
                + prep.condense_usage.input_tokens,
                output_tokens=(usage.output_tokens if usage else 0)
                + prep.condense_usage.output_tokens,
                embedding_tokens=prep.retrieval.query_tokens,
            ),
            retrieval_query=prep.retrieval_query,
            invalid_citations=resolved.invalid_markers,
            latency_ms=round((time.monotonic() - prep.started) * 1000),
        )

    def _messages(self, prep: PreparedAnswer) -> list[ChatMessage]:
        return self._prompts.answer_messages(prep.question, prep.context, prep.history)

    async def generate(self, prep: PreparedAnswer) -> RagAnswer:
        if not prep.context.sources:
            return self._finish(prep, "", model=None, usage=None)
        try:
            result = await self._llm.generate(
                self._messages(prep),
                max_output_tokens=self._options.max_output_tokens,
                temperature=self._options.temperature,
            )
        except ProcessingError as exc:
            raise _llm_unavailable(exc) from exc
        return self._finish(prep, result.text, model=result.model, usage=result.usage)

    async def stream_prepared(self, prep: PreparedAnswer) -> AsyncIterator[AnswerEvent]:
        """Stream answer text, then one ``AnswerCompleted`` with the verified result.

        The beginning of the output is buffered until it can no longer be the
        insufficient-context sentinel, so the sentinel itself is never shown.
        Model failures raise ``ServiceUnavailableError`` from the iterator.
        """
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
                self._messages(prep),
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

    async def summarize(
        self,
        ctx: WorkspaceContext,
        document_id: uuid.UUID,
        *,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> RagAnswer:
        """Summarize a document (or a page range) for reading mode, with citations.

        The context is the requested passages in reading order rather than
        search results, so the summary covers exactly what the reader is on.
        """
        ctx.require(Permission.CONVERSATIONS_USE)
        ctx.require(Permission.DOCUMENTS_READ)
        started = time.monotonic()
        if page_start is not None and page_end is not None and page_end < page_start:
            raise ValidationFailedError("The last page must not be before the first page.")
        repo = self._retrieval.repository
        found = await repo.documents(ctx.workspace_id, {document_id})
        doc = found.get(document_id)
        if doc is None:
            raise NotFoundError("Document not found.")
        if doc.archived or doc.status != "ready":
            raise ConflictError(
                "Only ready, non-archived documents can be summarized.", code="document_not_ready"
            )
        chunks = await repo.document_chunks(
            ctx.workspace_id, document_id, page_start=page_start, page_end=page_end, limit=200
        )
        context = build_context(chunks, max_tokens=self._options.context_max_tokens)
        if page_start is None:
            scope = "this document"
        elif page_end is None or page_end == page_start:
            scope = f"page {page_start}"
        else:
            scope = f"pages {page_start} to {page_end}"
        question = f"Summarize the key points of {scope}."
        warnings: list[SearchWarning] = []
        if context.dropped:
            warnings.append(
                SearchWarning(
                    "summary_partial",
                    "This part is long; the summary covers its beginning. Choose fewer pages.",
                    document_id,
                )
            )
        prep = PreparedAnswer(
            question=question,
            retrieval_query=question,
            retrieval=RetrievalResult(chunks, warnings, "vector", "", 0),
            context=context,
            history=[],
            started=started,
        )
        return await self.generate(prep)

    async def answer(
        self,
        ctx: WorkspaceContext,
        question: str,
        *,
        document_ids: Sequence[uuid.UUID] | None = None,
        collection_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
    ) -> RagAnswer:
        request = AnswerRequest(question, document_ids, collection_ids, top_k)
        return await self.generate(await self.prepare(ctx, request))

    async def stream(
        self,
        ctx: WorkspaceContext,
        question: str,
        *,
        document_ids: Sequence[uuid.UUID] | None = None,
        collection_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """Authorize and retrieve now; return the event stream for generation."""
        request = AnswerRequest(question, document_ids, collection_ids, top_k)
        return self.stream_prepared(await self.prepare(ctx, request))
