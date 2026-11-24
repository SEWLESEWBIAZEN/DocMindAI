"""Semantic search and grounded question answering (workspace-scoped)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from documind.api.dependencies import (
    RAGOrchestratorDep,
    RetrievalServiceDep,
    WorkspaceCtx,
    limit_ai_requests,
)
from documind.api.schemas.rag import (
    AnswerResponse,
    AskRequest,
    CitationResponse,
    SearchHitResponse,
    SearchRequest,
    SearchResponse,
    UsageResponse,
    WarningResponse,
)
from documind.application.rag.orchestrator import RagAnswer
from documind.application.rag.retrieval import SearchWarning

router = APIRouter(
    prefix="/workspaces/{workspace_id}",
    tags=["search"],
    dependencies=[Depends(limit_ai_requests)],
)


def _warnings(items: tuple[SearchWarning, ...] | list[SearchWarning]) -> list[WarningResponse]:
    return [
        WarningResponse(code=w.code, message=w.message, document_id=w.document_id) for w in items
    ]


def answer_response(result: RagAnswer) -> AnswerResponse:
    return AnswerResponse(
        answer=result.answer,
        status=result.status,
        citations=[
            CitationResponse(
                number=c.number,
                document_id=c.document_id,
                document_name=c.document_name,
                chunk_id=c.chunk_id,
                page_start=c.page_start,
                page_end=c.page_end,
                heading_path=list(c.heading_path),
                excerpt=c.excerpt,
                label=c.label,
                spoken_label=c.spoken_label,
            )
            for c in result.citations
        ],
        warnings=_warnings(result.warnings),
        model=result.model,
        prompt_version=result.prompt_version,
        retrieval_mode=result.retrieval_mode,
        sources_considered=len(result.sources),
        usage=UsageResponse(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            embedding_tokens=result.usage.embedding_tokens,
        ),
        latency_ms=result.latency_ms,
    )


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Semantic (hybrid) search across the workspace's ready documents",
)
async def search(
    body: SearchRequest, ctx: WorkspaceCtx, retrieval: RetrievalServiceDep
) -> SearchResponse:
    result = await retrieval.retrieve(
        ctx,
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids or None,
        collection_ids=body.collection_ids or None,
    )
    return SearchResponse(
        query=" ".join(body.query.split()),
        mode=result.mode,
        embedding_model=result.embedding_model,
        results=[
            SearchHitResponse(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                document_title=c.document_title,
                chunk_index=c.chunk_index,
                text=c.text,
                page_start=c.page_start,
                page_end=c.page_end,
                heading_path=list(c.heading_path),
                score=c.score,
                vector_similarity=c.vector_similarity,
                keyword_rank=c.keyword_rank,
            )
            for c in result.chunks
        ],
        warnings=_warnings(result.warnings),
    )


@router.post(
    "/ask",
    response_model=AnswerResponse,
    summary="Answer a question from the workspace's documents, with citations",
    description=(
        "Retrieves relevant passages, then generates an answer that cites them as [n]. "
        "Every citation is verified against the passages actually provided; markers "
        "that don't match a passage are removed. When the documents don't contain the "
        'answer, `status` is "insufficient_context" and no citations are returned.'
    ),
)
async def ask(body: AskRequest, ctx: WorkspaceCtx, rag: RAGOrchestratorDep) -> AnswerResponse:
    result = await rag.answer(
        ctx,
        body.question,
        document_ids=body.document_ids or None,
        collection_ids=body.collection_ids or None,
        top_k=body.top_k,
    )
    return answer_response(result)
