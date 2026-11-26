# ADR 0039: Account self-service: email change, data export, deleting accounts and organizations

- Status: Accepted
- Date: 2026-11-27
- Builds on: ADR 0013 (2FA-protected deletion), ADR 0021 (data planes), ADR 0026 (2FA),
  ADR 0032 (billing), ADR 0036 (SSO)

## Context

People couldn't change their email, get a copy of their data, or delete their account, and
owners couldn't delete an organization. Each of these needed support. They are also legal
expectations under GDPR and similar laws (access, portability, erasure), so EU customers
will ask for them before signing.

## Decision

### Changing email

- **Request:** Profile → Email takes the new address and the current password. People with
  2FA confirm a recent code too.
- **Confirmation:** a link (24 h) goes to the new address. The account changes only when it's
  opened, which also verifies the new address.
- **Notices:** the old address is told when a change is requested and again when it's done,
  so a hijacker can't quietly move the account.
- **No account discovery:** the answer is the same when the new address already has an
  account. That address gets a "nothing was changed" notice instead.
- **SSO accounts:** an account whose organization enforces single sign-on for its email domain
  can't change email itself (`409 email_managed_by_sso`). The organization manages it.
- **Audit:** the old and new domains are recorded, not the addresses.

### Download my data

- **Requesting:** Profile → Your data queues a background job; there's one at a time per
  person, and at most 3 a day.
  - The job writes one ZIP to the private application bucket and emails the person.
  - The archive downloads through a 5-minute signed link, only for its owner, for 7 days
    (`DATA_EXPORT_TTL_DAYS`).
  - An hourly sweep deletes expired archives and reports jobs that never finished.
- **Contents:**
  - `account.json`: profile, which sign-in methods are on (never secrets), workspaces and
    roles.
  - Per workspace: their own conversations (questions, answers, sources, their ratings) and
    the documents they uploaded.
  - Their personal workspace's original files, up to 2 GiB (`DATA_EXPORT_MAX_FILE_BYTES`).
  - Files they uploaded to an organization are listed, not copied: they belong to the
    organization.
- **Customer databases:** conversations are read from each workspace's own data plane, so
  workspaces on a customer's database are included.

### Deleting an account

- **Confirmation:** Profile → Delete account needs:
  - the password;
  - a recent 2FA code if 2FA is on;
  - `DELETE` typed.
- **Last owner:** refused while the person is the only owner of an organization
  (`409 last_owner`, naming them). They hand over ownership or delete the organization
  first, so no organization is left without an owner.
- **What's deleted:**
  - the account, its sessions, tokens and passkeys;
  - the personal workspace with its documents and files, after its subscription is
    cancelled;
  - their private conversations, ratings and collection memberships in every organization.

  In the shared database the foreign keys do this. Customer databases (ADR 0021) are cleaned
  explicitly.
- **What stays:** documents they uploaded to an organization stay with the organization (the
  uploader becomes empty). The audit trail keeps an entry with the user id.
- **Afterwards:** a confirmation email goes to the address; the browser signs out.

### Deleting an organization

- **Who:** owners, from Settings → Delete organization.
- **Confirmation:** the name typed exactly, and a recent 2FA code. 2FA is required, as for
  permanent document deletion (ADR 0013).
- **What's deleted:** everything in it: documents and files, collections, conversations,
  extraction, API keys, Slack and Drive connections, SSO and invitations. Members keep their
  own accounts.
- **Afterwards:** members are emailed.

### Order and safety (both deletions)

1. **Refuse what can't be undone safely.** A workspace on its own database or storage is
   refused with `409 custom_infrastructure`: move it back to DocuMind's infrastructure first.
   DocuMind never drops a customer's database or empties their bucket on its own.
2. **Stop billing first.** Stripe is asked for every live subscription of the customer, not
   only the one on record, because a webhook may be late (ADR 0032). Each is cancelled and
   open checkout pages are expired. If Stripe can't be reached, nothing is deleted
   (`503 billing_unavailable`).
3. **Delete the rows in one transaction**, with an audit entry `workspace.deleted` or
   `user.deleted`.
4. **Delete the stored files after the commit.** A failed delete leaves a logged orphan in
   the private bucket, never a document pointing at nothing.

Deleting is immediate, with no grace period. Losing an account by accident needs the
password, 2FA and a typed confirmation, and a grace period would keep personal data longer
than asked.

## Alternatives

- **A 30-day "scheduled deletion" with undo.** Rejected for now: it keeps data after a person
  asked for it to be erased, and it needs a second "really delete" job. It can be added if
  customers ask for it.
- **Exporting synchronously in the request.** Rejected: personal workspaces can hold gigabytes.
- **Revoking third-party tokens (Slack, Google Drive) on deletion.** Not done yet: the tokens
  are deleted with the workspace, so DocuMind can't use them. The apps still appear in the
  customer's Slack and Google settings until removed there.

## Consequences

- People manage their own email, data and account, which removes common support requests and
  meets the main GDPR rights.
- Schema only: `account_tokens.new_email` and the purpose `email_change`, and a new table
  `data_exports` (migration 0023). No existing rows change.
- Organizations on custom infrastructure need an extra step (moving back) before deletion.
- Deleting accounts that were created by SSO needs a password: such people first set one
  with "Forgot password".
