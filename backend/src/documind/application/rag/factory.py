"""Build retrieval and answering from settings (shared by the API and the worker)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.ai.resolver import WorkspaceAI
from documind.application.rag.condense import QueryCondenser
from documind.application.rag.orchestrator import GenerationOptions, RAGOrchestrator
from documind.application.rag.retrieval import RetrievalOptions, RetrievalService
from documind.core.config import Settings


def retrieval_service(session: AsyncSession, s: Settings, ai: WorkspaceAI) -> RetrievalService:
    return RetrievalService(
        session,
        ai.embeddings,
        RetrievalOptions(
            top_k=s.retrieval_top_k,
            candidates=s.retrieval_candidates,
            min_similarity=s.retrieval_min_score,
            hybrid=s.retrieval_hybrid,
            ef_search=s.retrieval_ef_search,
        ),
    )


def rag_orchestrator(retrieval: RetrievalService, s: Settings, ai: WorkspaceAI) -> RAGOrchestrator:
    return RAGOrchestrator(
        retrieval,
        ai.llm,
        GenerationOptions(
            max_output_tokens=s.llm_max_output_tokens,
            temperature=s.llm_temperature,
            context_max_tokens=s.rag_context_max_tokens,
            history_max_turns=s.conversation_history_turns,
            history_max_tokens=s.conversation_history_max_tokens,
        ),
        condenser=QueryCondenser(
            ai.llm, enabled=s.conversation_condense_followups, temperature=s.llm_temperature
        ),
    )
