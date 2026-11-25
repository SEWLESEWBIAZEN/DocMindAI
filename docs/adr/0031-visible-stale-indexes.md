# ADR 0031: Make documents on an old embedding model visible

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0019 (BYOK and re-indexing)

## Context

Search by meaning compares the question's embedding with each chunk's embedding. Embeddings from
two different models live in unrelated vector spaces, so they can't be compared, even when they have
the same number of dimensions. Retrieval therefore uses only chunks embedded with the workspace's
current model (`document_chunks.embedding_model`).

When the model changes, for example from the local stub to OpenAI, documents processed before the
change silently drop out of search by meaning. In hybrid mode they can still match by exact words.
A user saw answers that used only their newest document and could not tell why: the documents list
said "Ready" for all of them.

## Decision

Nothing about retrieval changes. We make the gap visible and easy to close:

- **Document flag:** document responses carry `needs_reindex`, which is true for a ready document
  with chunks from another model. The list response also has `needs_reindex_total` for the workspace
  (ready, not archived). The UI shows a "Needs re-indexing" badge in the list and the viewer.
- **Banner and "Re-index all":** the documents page shows a banner when `needs_reindex_total > 0`.
  Owners and admins (`ai_providers.manage`) get a "Re-index all" button that calls the existing
  `POST /workspaces/{id}/reindex`. Other members are told to ask an admin, or to reprocess
  documents one by one.
- **Search and answer warning:** search, ask and conversation answers add a warning
  `documents_need_reindex` with `count` and up to three titles when documents in the search scope
  are on another model. It says whether they were matched only by exact words (hybrid) or not
  searched (vector). The chat shows it next to the answer with a link to the banner, so an
  "insufficient context" answer is no longer mistaken for "your documents don't say this".
- **Unknown model:** if the workspace's AI setup can't be resolved (for example an invalid key),
  nothing is flagged, and document pages keep working.

## Consequences

- Two small extra queries on document list and detail, one on search: indexed lookups on
  `document_chunks` by workspace and document.
- The model's prompt is unchanged. The warning is shown to the user rather than given to the model.

## Database impact

None. No migration.

## Verification

- `tests/unit/test_reindex_warning.py`: wording for one or many documents, hybrid and vector.
- `tests/integration/test_rag_api.py::test_documents_on_another_model_are_reported_not_silently_skipped`:
  flag, total, search and answer warnings, scoped search without the stale document, and the flag
  cleared after reprocessing.
