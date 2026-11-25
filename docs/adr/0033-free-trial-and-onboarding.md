# ADR 0033: Free trial and first-run onboarding

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0032 (pricing and Stripe billing)

## Context

New users arrive with no documents. The first useful answer needs an upload, processing and a
good question, and many leave before that. Paid plans also needed a low-risk way to try them.

## Decision

### Free trial of Pro

- **Who gets it:** a workspace's first Pro subscription starts with a `BILLING_TRIAL_DAYS`-day (14)
  trial. It goes through Stripe Checkout with `payment_method_collection=if_required`, so no card
  is needed, and `trial_settings.end_behavior.missing_payment_method=cancel`.
- **Limits:**
  - one trial per workspace, and one per person, recorded in `billing_subscriptions.trial_user_id`
    and `trial_ends_at`;
  - Business has no trial, because a dedicated database costs money to provision;
  - contract plans get no trial.
- **Ending:**
  - Stripe's `customer.subscription.trial_will_end` event (three days before) emails the owners;
  - without a card the subscription is cancelled, and the existing webhook path returns the
    workspace to Free. Nothing is deleted.
- **Two-factor:** during the trial the workspace is on a paid plan, so the existing 2FA
  requirement for paid plans applies, with its grace period.
- **Where it shows:** the public catalog (`trial_days`) and the Billing page
  (`trial_days_available`, `trial_ends_at`) show the trial.

### Sample documents

- **What they are:** three original, fictional documents bundled with the backend
  (`documind/resources/samples`): a services agreement, an employee handbook and a product FAQ.
  They're written to have clear obligations, deadlines and facts.
- **Adding them:** `POST /workspaces/{id}/samples` adds them through the normal upload pipeline:
  storage, malware scan, processing, plan quotas checked before each file, and audit. They go into
  a "Sample documents" collection. Adding is idempotent, because duplicate content is already
  refused.
- **Recognising them:** a sample is identified by its content hash, with a single version.
  Documents get `is_sample`, and the UI shows a "Sample" badge.
- **Removing them:** `DELETE /workspaces/{id}/samples` removes unchanged samples without the 2FA
  step that deleting real documents needs (ADR 0013), because they hold no user data. A sample with
  a user-uploaded version is no longer a sample and stays. The collection is removed when empty.
- **Suggested questions:** the chat suggests questions the samples can answer. A test checks that
  each one gets a cited answer even from the offline development model.

### Getting-started checklist

The dashboard shows a dismissible checklist:
- add documents (upload, or try the samples);
- ask a first question;
- invite the team (organizations);
- turn on 2FA.

It's computed from existing data. Dismissal is remembered per browser.

## Consequences

- Samples count towards document and storage quotas like any upload. They are small (about 7 KB).
- Trial abuse is limited per person and per workspace, not per card. Someone with many accounts
  can still start several trials. Stripe Radar or a card-required trial are the options if that
  becomes a problem.

## Database impact

Migration 0017 is schema only. It adds two nullable columns to `billing_subscriptions`.

## Verification

- **`tests/integration/test_onboarding_api.py` (4 tests):** loading, answering and removing
  samples; changed samples are kept; permissions; quotas; every suggested question is answered.
- **`tests/integration/test_billing_api.py::test_free_pro_trial_once_per_workspace_and_person`:**
  covers the trial.
- **`e2e/onboarding.spec.ts`:** the checklist, samples, a cited answer from a suggested question,
  and removal, checked with axe.
