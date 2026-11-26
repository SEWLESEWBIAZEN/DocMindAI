# Runbook: billing with Stripe

Self-serve billing is optional (ADR 0032). Without Stripe settings the pricing page still shows the
plans, and operators assign plans with `documind-manage plan assign`.

## 1. Set up Stripe

Use test mode first (`sk_test_…`); switch to live keys only when everything below works.

1. **Products and prices.** Create two products, *DocuMind Pro* and *DocuMind Business*, each with
   two recurring prices, **per unit** (one unit = one member):

   | Setting | Pro | Business |
   | --- | --- | --- |
   | Monthly | $19.00 per unit, every month | $99.00 per unit, every month |
   | Yearly | $190.00 per unit, every year | $990.00 per unit, every year |

   The amounts must match `domain/billing/plans.py`, which is what the pricing page shows.
2. **Webhook.** Add an endpoint `https://YOUR_HOST/api/v1/billing/webhook` with these events:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `customer.subscription.trial_will_end` (the "trial ends soon" email)

   Copy its signing secret (`whsec_…`).
3. **Customer portal.** In the portal settings, allow updating payment methods, viewing invoices and
   cancelling (at the end of the period). **Turn off "switch plans"**: plan changes go through
   DocuMind, which checks that the workspace's storage and database fit the new plan.
4. **Key.** A restricted key (`rk_…`) is enough, with write access to Customers, Checkout Sessions,
   Customer portal and Subscriptions.

## 2. Configure DocuMind

In `.env` (development) or `deploy/production/.env`:

```bash
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO_MONTHLY=price_...
STRIPE_PRICE_PRO_ANNUAL=price_...
STRIPE_PRICE_BUSINESS_MONTHLY=price_...
STRIPE_PRICE_BUSINESS_ANNUAL=price_...
SALES_EMAIL=sales@example.com
```

Restart `api`, `worker` and `beat`. For local testing, forward events with the Stripe CLI.
Newer versions require the events to be listed. It prints a `whsec_…` to use as
`STRIPE_WEBHOOK_SECRET`:

```bash
EVENTS=checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,customer.subscription.trial_will_end
stripe listen --events "$EVENTS" -f http://localhost:8000/api/v1/billing/webhook
```

Without installing the CLI, run it in Docker on the Compose network (Windows `cmd`: one line, with
the events written out):

```bash
docker run --rm -it --network documind_default stripe/stripe-cli listen \
  --api-key sk_test_... --events "$EVENTS" -f http://backend:8000/api/v1/billing/webhook
```

## 3. How it behaves

- **Free trial (ADR 0033):** a workspace's first Pro subscription starts with a
  `BILLING_TRIAL_DAYS` (14) day trial, without a card. It is offered once per workspace and once
  per person. Stripe sends `trial_will_end` three days before the end, and owners are emailed. If
  no card was added, the subscription is cancelled at the end and the workspace returns to Free.
- **Upgrading:** an owner chooses a plan on *Billing*; Stripe Checkout takes the payment; the
  webhook assigns the plan. Paid plans turn on required two-factor sign-in, with the usual grace
  period for members.
- **Members:** the quantity follows the member count, checked hourly by the worker
  (`documind.sync_billing_seats`), prorated by Stripe.
- **Changing plan:** done in DocuMind (prorated). A change is refused if the workspace uses
  storage or a database the target plan doesn't include
  (`409 plan_lacks_infrastructure`).
- **Failed payment:** the subscription becomes `past_due`; the plan stays while Stripe retries, and
  owners are emailed.
- **Cancellation:** at the end of the period the workspace moves to Free. Nothing is deleted. If it
  still uses custom storage or a database, it keeps working, owners are emailed, the event is
  audited as `billing.over_plan`, and the Billing and Infrastructure pages ask them to move the data
  back or subscribe again. New infrastructure changes are blocked by the plan check.
- **Contracts:** a paid plan assigned by an operator (`documind-manage plan assign`) is a contract.
  Owners can't change it online, and a stray subscription never overrides it.

## 4. Operations

- **Events not arriving:** in Stripe, the webhook's event log shows the response. `422
  invalid_signature` means the secret is wrong; `503 billing_unavailable` means DocuMind couldn't
  reach Stripe to read the subscription, and Stripe retries automatically.
- **Webhook missed or late:** coming back from Checkout, the Billing page reads the
  subscription from Stripe (`POST /workspaces/{id}/billing/sync`), so the plan changes even
  without the webhook. Owners can also click **Refresh from Stripe**.
- **No double billing:**
  - Starting a checkout locks the workspace's billing record.
  - The checkout first asks Stripe whether the customer already has a live subscription. If so, it
    applies that subscription and refuses with `409 subscription_exists`.
  - Earlier unpaid Checkout pages are expired, so only the newest can be paid.
  - If a second live subscription still appears, it is cancelled at once, audited
    (`billing.duplicate_cancelled`) and the owners are emailed. Refund its first payment in
    Stripe if it was already charged. Cancelling stops future charges but doesn't refund.
- **Replays are safe:** handled event ids are stored in `billing_events`, and each event re-reads
  the subscription's current state from Stripe, so order doesn't matter.
- **Find a workspace's subscription:**
  `SELECT * FROM billing_subscriptions WHERE workspace_id = '…';`
- **Over-plan workspaces:**
  `SELECT workspace_id, details FROM audit_logs WHERE action = 'billing.over_plan' ORDER BY id DESC;`
- **Deleting a workspace with a live subscription:** cancel the subscription in Stripe first.
