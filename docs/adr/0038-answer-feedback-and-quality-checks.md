# ADR 0038: Answer feedback and quality checks

- Status: Accepted
- Date: 2026-11-27
- Builds on: ADR 0016 (private conversations), ADR 0021 (data planes), ADR 0033 (sample documents)

## Context

Answer quality is what people judge DocuMind on, and until now it was unmeasured.
There was no way for people to say an answer was wrong, and no repeatable check that a
change (a new model, a new prompt, retrieval settings) didn't make answers worse.

## Decision

### Feedback from people

- **Rating:** under every finished answer there is a thumbs up and a thumbs down.
  - A thumbs down opens an optional form with fixed reasons: wrong or made up,
    incomplete, sources don't support it, missed a document, hard to understand,
    something else. There's also a free-text comment (up to 1,000 characters).
  - One rating per answer. Rating again replaces it; clicking the active thumb removes it.
  - Only the person who asked can rate, through the same ownership check as the rest of
    the conversation API. Anyone else gets `404`.
- **Privacy:** conversations stay private (ADR 0016).
  - By default, admins see only counts.
  - In an organization the form has a "Share this question and answer with workspace
    admins" box, unticked by default. Only rated answers with that box ticked show their
    question and answer to admins, and never who rated them.
  - A personal workspace has no one to share with, so the box isn't shown.
- **Admin page "Answer quality"** (`usage.read`: owners and admins), for 7, 30 or 90 days:
  - share rated helpful, share of answers rated, and share of answers that found nothing
    relevant (`insufficient_context`);
  - how often each reason was given, and up/down per model;
  - the shared ratings, newest first (at most 50).
- **Storage:** `message_feedback` lives in the workspace's data plane next to the messages it
  rates, with a composite foreign key so a rating can't point into another workspace.
  Deleting the conversation deletes its ratings.
  - API: `PUT` / `DELETE /workspaces/{id}/conversations/{cid}/messages/{mid}/feedback`, and
    `GET /workspaces/{id}/answer-quality?days=`.
  - Messages carry the asker's own `feedback`.
  - API keys can rate, but can't read the admin summary.

### Repeatable quality checks

- **Command:** `documind-manage eval WORKSPACE_ID [--set samples|FILE] [--as EMAIL]
  [--json FILE] [--markdown FILE] [--min-pass 0.8]`.
  - It asks every question in a set against that workspace's documents, as the owner
    (or `--as` a member), with the workspace's own AI settings. Nothing is saved.
  - It prints PASS/FAIL per question and a Markdown summary, and exits `1` when the pass
    rate is below `--min-pass`, so CI or a release script can stop on a regression.
- **Built-in set `samples`:** 16 questions over the three sample documents. 13 have known
  answers (contract terms, leave policy, product FAQ) and 3 are not in the documents
  (the answer must decline, not guess).
- **Scoring is strict and explainable.** An answerable case passes only if all three
  hold:
  - the answer is grounded;
  - it cites one of the expected documents;
  - it contains every expected fact. A fact can list alternatives, such as
    `["2-year", "2 years"]`.

  An unanswerable case passes only if the answer says the documents don't cover it.
- **Your own sets:** a JSON file with `cases`, each with `question`, `documents`,
  `must_include`, or `"answerable": false`. Use one for your own document set (see
  `resources/evals/samples.json`).

## Alternatives

- **LLM-as-judge scoring.** Rejected for now: it costs a model call per case, varies
  between runs, and hides why a case failed. It can be added later as a separate check
  next to these deterministic ones.
- **Showing admins every rated conversation.** Rejected: it breaks the promise that
  conversations are private. Consent is per answer instead.
- **Storing feedback in the control plane.** Rejected: ratings sit next to the messages
  they describe, so customer-hosted data planes keep their content.

## Consequences

- People can tell us when an answer is wrong, and admins can see trends without
  reading private conversations.
- A model, prompt or retrieval change can be checked in minutes before release:
  `documind-manage eval <test workspace> --min-pass 0.9`.
- With the development stack's local model the sample set passes 16/16. That model is
  extractive, so this proves the plumbing, not answer quality. **Run the set with the real
  model before launch** and after any model or prompt change.
- Feedback isn't yet used automatically (for example to tune retrieval); it's a signal
  for people.
