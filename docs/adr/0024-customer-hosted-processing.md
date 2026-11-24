# ADR 0024: Customer-hosted processing (MI-7)

- Status: Accepted
- Date: 2026-11-24
- Builds on: ADR 0021 (infrastructure profiles), ADR 0022 (credential vault)

## Context

Some organizations need documents processed inside their own network. Extraction, OCR and embedding
read the full document text, and regulation or contracts may forbid that leaving their premises. The
data plane can already be the customer's own database (MI-4) and storage (MI-2).

## Decision

- **Pull, not push.** A customer worker (`documind-worker`, image target `customer-worker`) runs next
  to the customer's database and bucket.
  - It claims due jobs **directly in the customer's database**, using the same atomic claim, leases,
    retries and dead-lettering as DocuMind's workers.
  - The customer's database and storage credentials never leave their network.
  - DocuMind needs no inbound access to the worker.
- **Control plane contact is minimal and authenticated.** The worker calls:
  - `POST /worker/v1/heartbeat`: returns the workspace, scopes and the **embedding model the workspace
    searches with**;
  - `POST /worker/v1/usage`: records usage as `billed_to=workspace_key`, provider `customer_worker`,
    idempotent per job and outcome.

  Tokens are random (`dmw_…`), shown once, stored as SHA-256 and revocable. A revoked token stops the
  worker at its next heartbeat. Guessing is rate-limited per address.
- **Embedding consistency.** The worker refuses to run if its embedding model differs from the
  workspace's. Otherwise search would silently miss documents.
- **Modes:**
  - `customer`: DocuMind never dispatches the jobs, and its sweeper only recovers leases and creates
    missing jobs.
  - `hybrid`: both DocuMind's and the customer's workers may claim jobs; the claim is atomic.

  Both modes require an **external** database, and validation requires a registered worker that has
  checked in within the last 15 minutes.

## Alternatives

- **Push jobs to an inbound endpoint on the customer's side.** Rejected: it needs inbound firewall
  rules, and DocuMind would hold network reach into customer networks.
- **Workers using DocuMind's API for data (no database access).** This would work for organizations on
  DocuMind's data plane, but every document would transit DocuMind, which defeats the purpose.

## Consequences

- Customers operate workers, including upgrades in step with DocuMind releases and the schema.
- Figure descriptions aren't produced by customer workers yet: they would need a vision key on the
  customer side.
- Usage shown for customer workers is reported by the worker. That is enough for visibility, since it
  is billed to the customer's own key, but it isn't independently measured.
