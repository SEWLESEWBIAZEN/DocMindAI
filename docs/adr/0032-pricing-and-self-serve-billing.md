# ADR 0032: Pricing tiers and self-serve billing with Stripe

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0020 (plans and quotas), ADR 0021 (infrastructure profiles), ADR 0026 (2FA on paid plans)
- Changes: ADR 0020's "payment processing is out of scope"

## Context

Plans, limits and feature flags existed (ADR 0020), including the infrastructure features: your own
object storage, a dedicated or external database, and processing on your own servers. Plans had no
prices, nothing showed them publicly, and only an operator could change a workspace's plan.

## Decision

### Tiers and prices

Prices are per member (seat), in US dollars. Yearly billing charges ten months:

| Plan | Monthly | Yearly | Infrastructure |
| --- | --- | --- | --- |
| Free | $0 | $0 | DocuMind's database and storage |
| Pro | $19 | $190 | + your own object storage (S3, MinIO, Azure Blob) |
| Business | $99 | $990 | + dedicated database (hosted by us) or your own PostgreSQL |
| Enterprise | quote | quote | + processing on your own servers; custom limits |

- **Source of truth:** prices sit next to limits in `domain/billing/plans.py`. Pricing does not go
  into the `plans` table: Stripe holds the billed amounts, and the runbook says how to create
  matching prices.
- **Public catalog:** `GET /api/v1/plans` serves the catalog without authentication. The `/pricing`
  page and a pricing section on the landing page show cards, a comparison table and an FAQ.
- **Error messages:** infrastructure errors name the lowest plan that includes the feature.

### Payments: Stripe Checkout and the customer portal

- **Hosted by Stripe:** card details never reach DocuMind. A `BillingGateway` port has a Stripe
  adapter (official SDK, async) and a fake for tests.
- **Owners only:** checkout, plan changes and the portal need `billing.manage`.
- **First subscription:** `POST /workspaces/{id}/billing/checkout` creates the Stripe customer if
  needed and returns the Checkout URL. Seats equal the member count.
- **Plan changes:** `POST /workspaces/{id}/billing/plan` changes the subscription's price
  (prorated) and applies the result straight away.
- **Portal:** `POST /workspaces/{id}/billing/portal` covers payment methods, invoices and
  cancellation. Plan switching in the portal is turned off.
- **Webhooks:** `POST /api/v1/billing/webhook`.
  - Signatures are verified with the endpoint secret, with Stripe's five-minute replay window.
  - Event ids go in `billing_events`, so a redelivery is a no-op.
  - Each subscription event **re-reads the subscription from Stripe**, so out-of-order delivery
    can't apply stale state.
- **Mapping to plans:**
  - `active`, `trialing` and `past_due` give the plan the price pays for;
  - `canceled`, `unpaid` and `incomplete_expired` give Free;
  - assignment goes through the audited `BillingService.assign` (`assigned_by = stripe`), keeping
    operator overrides;
  - cached entitlements and AI routing are dropped, so the change applies at once.
- **Contracts win:** a paid plan assigned by an operator isn't changed by Stripe, and owners can't
  start a checkout over it.
- **Seat sync:** a worker task runs hourly and sets each subscription's quantity to the member
  count.

### Custom infrastructure and plan changes

- **Online plan changes:** refused (`409 plan_lacks_infrastructure`) when the workspace uses a
  feature the target plan lacks. The Billing page shows the reason on each blocked plan.
- **Cancellation:** can't be refused. The workspace moves to Free but keeps working on its own
  storage and database. Nothing is cut off or deleted.
  - The event is audited (`billing.over_plan`), owners are emailed, and both pages show a banner.
  - The existing entitlement check blocks new infrastructure changes, except moving back to
    DocuMind.
- **Failed payment (`past_due`):** the plan stays while Stripe retries, and owners are emailed.

## Consequences

- **Enterprise:** stays sales-led (`SALES_EMAIL`) and assigned by operators.
- **Without Stripe settings:** the pricing pages still show prices, nothing is self-serve, and
  billing returns `503 billing_disabled`.
- **Seat counts:** can lag by up to an hour.
- **Deleting a workspace:** a workspace with a live subscription should have the subscription
  cancelled in Stripe first (runbook).

## Database impact

Migration 0016 is schema only. It adds `billing_subscriptions` (customer, subscription, status,
seats, period end) and `billing_events` (handled webhook ids). Existing plans are unchanged.

## Verification

- **Unit, `tests/unit/test_pricing.py`:** prices and the yearly discount; tier order of the
  infrastructure features; error messages; Stripe settings all-or-nothing; parsing of current and
  older subscription shapes; real webhook signature checks, including replays.
- **Integration, `tests/integration/test_billing_api.py`:** run with a fake gateway and real
  signatures. Covers:
  - the public catalog;
  - checkout, the webhook and idempotency;
  - invalid signatures;
  - plan change, seat sync, payment failure and cancellation;
  - custom storage with an external database on Business, a refused downgrade, and cancellation
    with the data still readable;
  - permissions, contract plans, and Stripe being down or not configured.
- **Vitest:** price formatting, infrastructure highlights and pricing section behaviour.
- **Playwright, `e2e/billing.spec.ts`:** the pricing page (axe, 390 px) and the in-app upgrade
  redirect.
