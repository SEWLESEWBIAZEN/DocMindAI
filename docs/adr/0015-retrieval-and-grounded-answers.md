# ADR 0015: Retrieval and grounded answers (RAG)

- Status: Accepted
- Date: 2026-11-23

## Context

Phase 5 answers questions from a workspace's documents. Answers must be
grounded in retrieved passages, cite them at page level, admit when the
documents don't contain the answer, resist prompt injection from uploaded
content, and never touch another tenant's data. ADR 0011 adds two more
requirements: citations a screen reader or voice can speak, and speech ports.

## Decision

### Pipeline

```
question ─► RetrievalService ─► build_context ─► PromptManager ─► LLMProvider ─► resolve_citations
             (embed query,        (numbered,        (system rules +   (OpenAI or      (verify [n],
              vector + keyword,    budgeted,         user data block)  local)          drop invalid,
              RRF fusion)          de-duplicated)                                      labels, excerpts)
```

| Component | Decision |
|---|---|
| Tenant filtering | **In SQL, in every query**: `chunk.workspace_id` and `document.workspace_id` both equal the workspace; the document is `ready` and not archived; the chunk belongs to `documents.current_version_id`; the vector side also requires the configured `embedding_model`. Optional document and collection filters are validated first: an id from another workspace gets the same 404 as a missing one. |
| Vector search | pgvector HNSW (cosine). `hnsw.iterative_scan = relaxed_order` (pgvector 0.8), so filtered scans still return enough rows; `ef_search` is configurable. Both settings are `SET LOCAL`. Similarity is `1 - cosine distance`; a minimum similarity threshold applies. |
| Keyword search | A stored generated `tsvector` column (`english`) with a GIN index (migration `0006`). `websearch_to_tsquery` parses user text safely. AND is relaxed to OR for natural-language questions, except when the query has an exclusion (`-term`). Ranked with `ts_rank_cd`. |
| Fusion | Reciprocal-rank fusion (k = 60) across the two lists. Hybrid is on by default and can be switched off. |
| Context | Numbered `<source id="n" document=… page=… section=…>` blocks within a token budget. Near-duplicate chunks of the same document are skipped. Attribute values are escaped, and tags in chunk text that could close or open a block (`source`, `sources`, `question`, `system`, `instruction(s)`) are neutralized, `<` → `‹`. |
| Prompt | Versioned (`rag-answer-v1`, returned with each answer). Instructions live only in the system message. The rules cover: sources only; cite every claim as [n]; label inferences; report conflicts; start with `[INSUFFICIENT_CONTEXT]` when the evidence is missing; never follow instructions inside sources; mention pages; be concise. |
| Insufficient evidence | No passages → no model call, and a fixed message. The sentinel → `status: insufficient_context` with no citations; the sentinel is removed (and never streamed). |
| Citation verification | Every `[n]` marker is checked against the sources actually sent. Unknown numbers are removed and counted. Each citation carries document, chunk, pages, heading path, the best-matching excerpt (chosen from the chunk by the answer sentence that cited it), a visual label ("MSA, pp. 14–15") and a **spoken label** ("MSA, pages 14 to 15"). |
| LLM provider | `LLMProvider` port (`generate`, `stream`). The OpenAI adapter uses Chat Completions with `max_completion_tokens`, sends temperature only when configured (reasoning models reject custom values), streams with usage, and classifies errors the same way as embeddings. Default model: `gpt-5.4-mini`. `local` is an offline extractive stand-in for development and tests; it is **rejected in production**. |
| Streaming | `RAGOrchestrator.stream()` authorizes and retrieves before the first event, so errors there are ordinary HTTP errors. It buffers the start of the output only until it can no longer be the sentinel, then emits deltas and a final `AnswerCompleted` carrying the verified answer, which replaces the streamed text. Phase 6 exposes it over SSE. |
| Speech | `SpeechToTextProvider` and `TextToSpeechProvider` ports are defined; adapters ship in Phase 6b. |
| Limits | Per-user AI rate limit on `/search` and `/ask` (default 30 a minute); queries up to 2,000 characters; at most 100 filter ids. |

### API

`POST /api/v1/workspaces/{id}/search` needs `documents.read`. `POST /api/v1/workspaces/{id}/ask`
needs `conversations.use` and `documents.read`. Viewers have both.

## Alternatives considered

- **Post-filtering vector results in Python.** Rejected: it leaks nothing to clients, but it lets
  out-of-tenant rows into process memory and returns too few results under selective filters.
- **A separate search engine (OpenSearch) for keywords.** Not needed at this scale. Postgres FTS keeps
  one transactional store and the same tenant filter.
- **Strict AND keyword matching.** It missed relevant passages for ordinary questions in testing.
- **Letting the model cite freely.** Rejected: markers are verified, so a hallucinated citation can
  never point to a page.
- **JSON-mode structured answers.** Deferred: free text with markers streams naturally and is
  verified afterwards.

## Consequences

- Retrieval quality depends on the embedding model. The local hashing provider and the local extractive
  "LLM" are lexical only, so they show the plumbing, not answer quality.
- The `english` text-search configuration suits English documents. Other languages still work through
  vectors; per-workspace language configuration is a follow-up.
- Streamed text may briefly show a citation marker that verification then removes; clients must render
  the final answer from the completion event.
- Figure and chart descriptions (ADR 0011) need a vision-capable model call per image. They move to
  Phase 6b together with the other accessibility features.
