# ADR 0034: Structured extraction, comparison and deadlines

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0014/0016 (grounded answers and citations), ADR 0020 (quotas), ADR 0021 (data planes)

## Context

People asked DocuMind questions like "what are the main obligations and deadlines?". A chat
answer is the wrong shape for that. They want a table to review, export and act on, and they want
to compare the same facts across several contracts.

## Decision

### Extraction runs

- **Templates:** `obligations` (a list of items, each with party, deadline, due date and
  recurrence), `contract_terms` (one value per document) and `invoice`. A `custom` template takes
  up to 12 fields of type text, date, amount or number, with either result shape.
- **Starting a run:** `POST /workspaces/{id}/extractions` takes documents or a collection (at
  most `EXTRACTION_MAX_DOCUMENTS`). It checks the chat quota, as answers do, and dispatches the
  Celery task `documind.run_extraction`. At most three runs per workspace can be active.
- **How documents are read:** the worker reads each document's chunks in order, in windows of
  about `EXTRACTION_WINDOW_TOKENS`, using the workspace's own AI routing and metering.
  - Each window is one call with the same `<source id="n">` blocks as answers, including the
    prompt-injection guard.
  - The system prompt asks for JSON only, with `null` for anything not stated.
- **Grounding:**
  - every value must name a valid source number, or it is dropped;
  - stored values keep the chunk, pages and an excerpt, and the UI links each value to the
    passage;
  - dates are kept as ISO dates only when the document states a full calendar date.
- **Unreadable replies:** they are retried once. If still unreadable, the document gets a warning
  and the run continues.
- **Very long documents:** only the first `EXTRACTION_MAX_CALLS_PER_DOCUMENT` windows are read, and
  the document is marked `partial`.
- **Combining windows:** in fields mode the first value in reading order wins. In items mode,
  duplicates are removed by main value and date.
- **Recovery:** a sweep every two minutes fails runs without a heartbeat for
  `EXTRACTION_STALE_SECONDS` (`interrupted`) and re-sends runs whose message was lost.
- **Export:** `GET …/extractions/{id}/export.csv`. Cells that start with `= + - @` are prefixed so
  spreadsheets don't run them as formulas.

### Comparison

For a fields run over several documents, the UI shows fields as rows and documents as columns, and
marks rows whose values differ.

### Deadlines

- **Tracking:** any extracted date can be tracked: `POST /workspaces/{id}/deadlines`, with title,
  due date, document, source, assignees (workspace members; the creator by default) and reminder
  days (default 7, 1 and 0).
- **Reminders:** an hourly worker task emails assignees once per reminder day. Missed reminders
  are caught up once. Changing the date resets them. The calendar day is in UTC.
- **Calendar:** `GET …/deadlines/calendar.ics` returns the open deadlines as all-day events.
- **Deleting a document** deletes its deadlines.

### Offline model

The development and test model (`LLM_PROVIDER=local`) recognises the `<extraction>` block and
extracts by rules: obligation verbs with time phrases, and keyword overlap for fields. Extraction
therefore works end to end without an AI key. Production rejects this model, as before.

## Consequences

- Extraction costs one or more model calls per document. It is metered and quota-checked like
  answers.
- Values are the model's reading of the document. The UI says so, and a source is always one
  click away.
- Runs execute on DocuMind's workers even for workspaces with customer-hosted processing, as
  answers do in the API. Results are stored in the workspace's own data plane.

## Database impact

Schema only: migration 0018 (shared) and dp0002 (data planes) add `extraction_runs`,
`extraction_results` and `deadlines`. No existing rows change. `documind-manage dataplanes migrate`
upgrades dedicated and external databases.

## Verification

- **`tests/unit/test_extraction.py` (9 tests):** parsing and grounding, custom fields, windows,
  the prompt-injection guard, the offline model, excerpts, CSV safety, reminders and the iCalendar
  file.
- **`tests/integration/test_extraction_api.py` (5 tests):**
  - obligations with sources, CSV and metering;
  - comparison over a collection, limits and isolation;
  - recovery of stuck runs;
  - deadlines, reminders and the calendar;
  - viewer permissions.
- **`e2e/extraction.spec.ts`:** from a document's menu through to a tracked deadline, with axe.
