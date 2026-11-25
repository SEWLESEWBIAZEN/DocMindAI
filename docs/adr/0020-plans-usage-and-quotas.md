# ADR 0020: Plans, entitlements, usage metering and quotas

- Status: Accepted
- Date: 2026-11-24
- Phase: 8

## Context

Usage is currently computed on the fly from operational tables (ADR 0015, Phase 7 usage page). Nothing
is metered at the point of use, so there is no basis for billing or for quotas. Some usage isn't
recorded anywhere: audio minutes (ADR 0018), figure descriptions, and whose key paid (ADR 0019).

## Decision

### Plans and entitlements

- A `plans` table holds a key, a name and an `entitlements` JSON document. The plan catalog in code
  (`domain/billing/plans.py`) is the source of truth, and migrations seed the table from it, as with
  roles.
- `workspace_plans` assigns one plan per workspace. Optional per-workspace `overrides` change
  individual entitlements, for example for an enterprise contract. Workspaces without a row are on
  **Free**.
- Entitlements are typed (`Entitlements` dataclass). Limits:
  - `max_members`, `max_documents`, `storage_bytes`;
  - monthly `chat_tokens`, `embedding_tokens`, `audio_seconds` and `figure_descriptions`.

  Feature flags:
  - `byok`, `platform_ai`, `server_voice`;
  - `custom_storage`, `dedicated_database`, `external_database`, `customer_workers`.

  `None` means unlimited.
- BYOK is enabled on every plan, including Free (ADR 0019 decision 3).
- Plans are assigned by operators (`documind-manage plan assign`), and every change is audited. Payment
  processing is out of scope. A billing integration would only write `workspace_plans`.
  *Update: ADR 0032 adds Stripe billing, which does exactly that.*

### Usage ledger

- `usage_records` is an append-only ledger in the control plane. Each row holds:
  - `workspace_id`, optional `user_id`;
  - `capability`: `embedding`, `chat`, `vision`, `speech_to_text`, `text_to_speech`;
  - `provider`, `model`;
  - `billed_to`: `platform` or `workspace_key`;
  - quantities: `input_tokens`, `output_tokens`, `audio_seconds`, `characters`, `units`;
  - `source_type` and `source_id`, `occurred_at`;
  - a unique `idempotency_key`, so a retried job or a re-delivered task never double-counts.
- Records hold no document content.
- Recording happens in the application layer through a `UsageMeter` port, right after a successful
  provider call:
  - embedding batches in processing;
  - answers and condensation;
  - document summaries;
  - figure descriptions;
  - transcription (audio duration);
  - speech (characters, with estimated seconds at 15 characters per second).
- Storage and document counts are measured from the data plane, not metered.

### Quotas

- Quotas are checked at admission, before work starts. Exceeding one returns **402 `quota_exceeded`**
  with the limit, the current use and the period end.
  - Uploads: `max_documents` and `storage_bytes`.
  - Questions and summaries: `chat_tokens`.
  - Server speech: `audio_seconds`.
  - Invitations: `max_members`.
- Only platform-billed usage counts toward AI quotas. Calls on the workspace's own key are recorded
  for visibility only (ADR 0019).
- Background work is never half-done because of a quota:
  - Embeddings for an accepted upload always complete.
  - Figure descriptions above the monthly quota are skipped, with a `figures_not_described:quota`
    warning.
- The period is the calendar month in UTC. Counting is a single indexed aggregate over
  `(workspace_id, capability, billed_to, occurred_at)`.
- Quotas are soft by about one request: two concurrent questions can both pass the check. This is
  accepted, because AI cost is bounded by per-request limits and rate limits.

## Consequences

- Usage figures are authoritative for billing from the date the ledger starts. Earlier activity is not
  backfilled, because operational totals don't record who paid.
- The usage page shows the plan, quota bars and per-capability totals, plus the existing activity
  charts.
- The 402 status is new to the frontend, and the UI explains it.
