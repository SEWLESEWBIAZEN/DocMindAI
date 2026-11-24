# ADR 0016: Conversations and answer streaming

- Status: Accepted
- Date: 2026-11-24
- Amends: ADR 0015 (source filters combine as a union; prompt `rag-answer-v2`)

## Context

Phase 6 turns one-shot answers (ADR 0015) into persistent conversations: users
continue them, choose the documents and collections they draw on, see answers
stream in, cancel them, and regenerate the latest one. Streaming must survive
real-world failure: clients that disconnect, models that fail mid-answer, and
API processes that die. An incomplete answer must never be stored as complete.

## Decision

### Data model (migration `0007`)

| Table | Notes |
|---|---|
| `conversations` | Workspace + **owner** (`created_by_user_id`), title (`auto` from the first question, or `user`), `scope` (`all` or `selected`), counters. |
| `conversation_documents`, `conversation_collections` | Selected sources. Composite `(id, workspace_id)` FKs to the workspace's documents and collections. Removed automatically when the source is deleted. |
| `messages` | `user` / `assistant`, `sequence` (unique per conversation), `status` `streaming` → `completed` \| `failed` \| `cancelled`, `reply_to_id`, answer metadata (status, model, prompt version, retrieval query and mode, token usage, latency, warnings, `error_code`), `superseded_at`. A **partial unique index** allows one `streaming` answer per conversation. |
| `message_citations` | A snapshot of each verified citation: document name, pages, section, excerpt and the visual and spoken labels. `document_id` uses `ON DELETE SET NULL (document_id)` (PostgreSQL 15+) and `chunk_id` uses `SET NULL`, so answers stay readable after deletion or re-processing. |

### Access

Conversations are **private to their creator** inside their workspace. Every
endpoint needs `conversations.use` (all roles have it). Another member, an
admin, another workspace or a removed member gets **404**, the same as for a
missing conversation. This covers reading, sending, regenerating, cancelling,
deleting and citations. Selecting sources needs `documents.read`, and every
id must belong to the workspace (404 otherwise). Deletion is audited, without
the title.

### Answering in a conversation

1. Lock the conversation row (`FOR UPDATE`) and fail its stale answers. If an
   answer is still streaming, return 409 `reply_in_progress`.
2. Prepare the answer before saving anything: condense the follow-up, retrieve
   with the conversation's scope, build the context. Errors here are ordinary
   HTTP errors, and nothing is stored.
3. Commit the question (`completed`) and the answer placeholder (`streaming`).
4. Generate the answer, streamed or not (see below), and finalize it in a
   **separate short transaction**.

**Memory.** Up to 6 earlier completed turns, about 2,000 tokens, go into the
prompt as user and assistant messages. Citation markers are stripped from
earlier answers, because they referred to earlier sources. Prompt
`rag-answer-v2` adds a rule: earlier turns help interpret the question but are
not sources.

**Follow-up condensation.** A follow-up ("what about the second one?") is
rewritten into a standalone retrieval query with a small versioned prompt
(`condense-v1`, 120 output tokens). If the model fails or returns something
unusable, the fallback is the previous question plus the current one. The
offline provider always takes the fallback. The query used is stored on the
answer.

**Scope.** `all` searches the whole workspace. `selected` searches the selected
documents **plus** the documents in the selected collections (a union; ADR
0015's search endpoint now uses the same semantics). If every selected source
is gone, the conversation searches **nothing**: the answer is
`insufficient_context` with warning `selection_empty`. It never widens
silently to the whole workspace.

**Regenerate.** Allowed only for the conversation's latest, non-superseded
answer, and not while it is streaming. The old answer is kept with
`superseded_at` set: hidden by default and listed with
`include_superseded=true`. Its history is the conversation before that
question.

### Streaming (SSE)

The same endpoints serve both modes: `Accept: text/event-stream` streams,
anything else waits and returns JSON. The event types are `message.created`,
`answer.delta`, `answer.completed` (the verified answer, which replaces the
deltas), `answer.cancelled` and `answer.error`. While the model is silent,
`: keep-alive` comments go out every 15 s. Responses send
`Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`.

The model runs in a producer task that feeds a queue. The response consumes
the queue and also:

- polls a **Redis cancel flag** every 0.5 s, so `POST …/cancel` works from any
  API process;
- touches the message's `updated_at` regularly as a liveness heartbeat;
- on cancel, failure or **client disconnect**, cancels the producer (which
  stops token generation) and stores the partial text as `cancelled`
  (`cancelled_by_user` or `client_disconnected`) or `failed` (the error code).
  These terminal writes are **shielded** from cancellation.

Every finalizing write is conditional on the message still being `streaming`.
**Crash recovery:** a beat task (`sweep_stale_replies`, every 60 s), plus a
check on each send, marks answers with no heartbeat for 5 minutes as `failed`
(`interrupted`).

## Alternatives considered

- **Conversations shared with the whole workspace.** Rejected as the default,
  because conversations often contain personal working notes. Explicit sharing
  and compliance access for admins can come later (Phase 8) as a policy, not as
  a different storage model.
- **Keep generating after a disconnect, so a reload shows the full answer.**
  Rejected: it spends tokens nobody asked for and needs detached background
  tasks that don't survive restarts. A disconnect cancels; the partial answer
  is kept and the user can regenerate.
- **WebSockets.** Unnecessary: the stream is one-way and cancellation is a
  normal HTTP call. SSE works through standard proxies and HTTP/2.
- **Resumable streams (`Last-Event-ID`).** Deferred. Clients reload the
  message instead; completed answers are always persisted.
- **Writing each delta to the database.** Too much write load. Only terminal
  states and periodic heartbeats are written.

## Consequences

- A long-running request holds no database connection while streaming: the
  request session commits before the stream starts, and each finalizing write
  takes a pooled connection briefly.
- A crashed API process leaves at most 5 minutes during which the conversation
  rejects new questions (409), and only that conversation is affected.
- Deltas can include citation markers that verification later removes. Clients
  must render `answer.completed`.
