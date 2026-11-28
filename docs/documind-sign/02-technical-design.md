# DocuMind Sign — 02. Technical design

- **Milestone:** M0 (design for review).
- **Basis:** [`01-architecture-discovery.md`](01-architecture-discovery.md).
- **Becomes:** ADR 0041 once approved.
- **Decisions:** C1–C4 and R11 approved 2026-11-28 (§8.2, §15). M0 approved; M1 started.

## 1. Scope of the initial release

**In scope (M1–M4):**
- PDF only.
- One document per agreement.
- Signers and approvers.
- Sequential, parallel and mixed (grouped) signing order.
- Drawn, typed and uploaded signatures.
- Email-link access, with an optional email code (M2).
- Finalization on the server: flattened signed PDF, SHA-256 hashes, completion certificate,
  append-only events.
- AI suggestions confirmed by a person (M3).

**Out of scope:** everything in prompt §18. In particular: Word conversion, SMS, PAdES and
certificate-backed signatures, qualified signatures, KYC, bulk sending.

## 2. Components

```
frontend/src/features/sign/
  SignaturesPage          dashboard (tabs, filters)
  AgreementWizard         steps 1–4 and 6–7
  FieldEditor             step 5: thumbnails | PDF.js canvas | recipients & fields
  AgreementDetailPage     status, recipients, timeline, evidence
  pdf/                    PdfDocument, PdfPage (pdfjs-dist), coordinate helpers
frontend/src/features/signing-portal/   public, outside the app shell: /sign
  SigningPortal, ConsentStep, FieldNavigator, SignaturePad, Done

backend/src/documind/
  domain/signing/          states, transitions, field types, coordinate maths (pure, unit-tested)
  application/signing/
    agreement_service.py   drafts, recipients, fields, send, void (WorkspaceContext)
    portal_service.py      token → session → view, consent, fields, sign, decline
    snapshot.py            source version → validated, flattened snapshot + page geometry
    finalizer.py           overlay, flatten, hash, certificate, document creation
    notifications.py       email rows, send, reminders, expiry
    evidence.py            events (hash chain), verification
  infrastructure/pdf/      pikepdf + reportlab adapters (behind a port)
  infrastructure/database/models/signing.py
  api/routes/agreements.py   /api/v1/workspaces/{ws}/agreements…
  api/routes/signing.py      /api/v1/sign/…  (public)
  worker/tasks.py            finalize, send notification, sweeps
```

The PDF adapters sit behind a port (`SnapshotBuilder`, `PdfStamper`, `CertificateRenderer`), so
certificate-backed signing with pyHanko can be added later without changing services.

## 3. Data model

All tables below except §3.2 are **data-plane** tables:
- added to `DATA_PLANE_TABLES`;
- created by migrations in **both** Alembic environments;
- covered by the parity test;
- keyed by `workspace_id` with composite `(id, workspace_id)` foreign keys, as documents are.

User references are plain UUIDs in dedicated planes, as today.

### 3.1 Data-plane tables

| Table | Key columns | Notes |
|---|---|---|
| `agreements` | `id`, `workspace_id`, `title`, `message`, `status`, `routing` (`sequential`/`parallel`/`groups`), `created_by_user_id` (SET NULL), `source_document_id`, `source_version_id` (composite FK to `document_versions`), `expires_at`, `sent_at`, `completed_at`, `voided_at`, `void_reason`, `signed_document_id`, `finalize_attempts`, `finalize_lease_until`, `lock_version`, `created_at`, `updated_at` | `status` is changed only by the transition service (§5). `lock_version` provides optimistic concurrency for draft edits |
| `agreement_files` | `id`, `agreement_id`, `workspace_id`, `kind` (`snapshot`/`final`/`certificate`/`signature`), `storage_key` (unique), `sha256`, `size_bytes`, `media_type`, `page_geometry` (JSONB, snapshot only), `created_at` | Partial unique index: one `snapshot`, one `final`, one `certificate` per agreement. Rows are never updated |
| `agreement_recipients` | `id`, `agreement_id`, `workspace_id`, `name`, `email` (stored lower-case), `role` (`signer`/`approver`), `group_order` (int ≥ 1), `status`, `auth_method` (`link`/`link+email_code`), `user_id` (nullable; set only by the C3 rule), `invited_at`, `viewed_at`, `consented_at`, `completed_at`, `declined_at`, `decline_reason`, `reminders_sent`, `last_reminded_at` | Unique `(agreement_id, email)` |
| `agreement_fields` | `id`, `agreement_id`, `workspace_id`, `recipient_id`, `type` (`signature`/`initials`/`full_name`/`date_signed`/`text`/`checkbox`/`company`), `page_index`, `x`, `y`, `width`, `height` (points, §6.3), `page_width`, `page_height`, `page_rotation`, `required`, `label`, `rules` (JSONB), `value`, `completed_at` | `value` is written once (§5.3). `date_signed` and `full_name` are filled by the server |
| `agreement_signatures` | `id`, `agreement_id`, `workspace_id`, `recipient_id`, `field_id` (**unique**), `method` (`drawn`/`typed`/`uploaded`), `file_id` → `agreement_files`, `typed_text`, `font`, `created_at` | The unique `field_id` makes overwriting impossible |
| `agreement_events` | `id`, `agreement_id`, `workspace_id`, `seq` (per agreement), `type`, `actor_type` (`user`/`recipient`/`system`), `actor_user_id`, `recipient_id`, `occurred_at` (server), `request_id`, `ip_address`, `user_agent`, `details` (JSONB), `prev_hash`, `hash` | Append-only (§9). Unique `(agreement_id, seq)` |
| `agreement_notifications` | `id`, `agreement_id`, `workspace_id`, `recipient_id`, `kind`, `idempotency_key` (**unique**), `status` (`pending`/`sent`/`failed`), `attempts`, `last_error`, `sent_at`, `created_at` | Outbox for email; the unique key stops duplicate sends (§10) |
| `signing_templates` (M3) | `id`, `workspace_id`, `name`, `description`, `definition` (JSONB: roles, order, fields relative to page), `created_by_user_id`, timestamps | Added after the core workflow is stable |

**No duplicated document metadata:** the source is referenced by `(source_document_id,
source_version_id)`, and the signed PDF becomes a normal document referenced by
`signed_document_id`.

### 3.2 Control-plane table: `signing_tokens`

| Column | Purpose |
|---|---|
| `token_hash` (PK, SHA-256) | Raw token never stored |
| `kind` (`invite`/`session`/`email_code_attempt`) | Invitation link, portal session, code challenge |
| `workspace_id` (FK workspaces, CASCADE) | Resolves the data plane |
| `agreement_id`, `recipient_id` (UUIDs) | Scope; validated again in the data plane |
| `expires_at`, `revoked_at`, `used_at`, `created_at` | Lifecycle |

The table holds no titles, emails or content. Deleting a workspace cascades its tokens. Voiding,
expiry, decline or reassignment revokes them.

## 4. API

The conventions are the existing ones:
- errors are `{"detail", "code"}`;
- request models use `extra="forbid"`;
- the OpenAPI contract and TypeScript types are regenerated;
- mutating cookie-authenticated portal requests send the CSRF header, as refresh and logout do.

### 4.1 Workspace API (`WorkspaceContext`; automatically covered by the tenant sweep)

| Method and path | Permission | Notes |
|---|---|---|
| `GET /workspaces/{ws}/agreements?tab=&q=&status=&recipient=&from=&to=&sort=&offset=&limit=` | `agreements.read` | Tabs: all, drafts, waiting_for_me, waiting_for_others, completed, declined, voided, expired |
| `POST /workspaces/{ws}/agreements` | `agreements.create` | `{title, message?, source: {document_id} or upload_id}`. Creates a DRAFT and snapshots the pinned version (§6) |
| `GET/PATCH/DELETE /workspaces/{ws}/agreements/{id}` | read / create (own draft) | PATCH and DELETE only while DRAFT; `lock_version` required |
| `PUT /workspaces/{ws}/agreements/{id}/recipients` | create (own draft) | Replaces the whole list (validated as a set: order, roles, unique emails) |
| `PUT /workspaces/{ws}/agreements/{id}/fields` | create (own draft) | Replaces the whole list; validated against the page geometry |
| `GET /workspaces/{ws}/agreements/{id}/snapshot` | read | Streams the snapshot PDF for PDF.js (no storage URL exposed) |
| `POST /workspaces/{ws}/agreements/{id}/send` | create (own) | `Idempotency-Key` header. DRAFT → SENT; creates invitations |
| `POST /workspaces/{ws}/agreements/{id}/void` | `agreements.void_own` / `void_any` | `{reason}`; revokes tokens; notifies |
| `POST /workspaces/{ws}/agreements/{id}/recipients/{rid}/resend` | create (own) | Rate-limited; new token; the old one is revoked |
| `GET /workspaces/{ws}/agreements/{id}/events` | `agreements.audit_own` / `audit_any` | |
| `GET /workspaces/{ws}/agreements/{id}/files/{final or certificate}` | read (completed) | Short-lived download link; logs an event |
| `POST /workspaces/{ws}/agreements/{id}/verify` | read | Recomputes stored files' SHA-256 and the event hash chain; returns per-item results |
| `POST /workspaces/{ws}/agreements/{id}/finalize` | `agreements.void_any` (operator) | Re-queues a FINALIZATION_FAILED agreement |
| Templates (M3) | `signing_templates.manage` | CRUD |

### 4.2 Public signing API (`/api/v1/sign`, no workspace in the URL)

| Method and path | Auth | Notes |
|---|---|---|
| `POST /sign/session` | Body `{token}` | Validates the invite token (hash lookup, not expired or revoked, agreement in a signable state, recipient's turn). Sets an HttpOnly, `SameSite=Strict`, `Path=/api/v1/sign` session cookie (`kind=session`, 2 h). The invite token stays valid until the recipient finishes (it can reopen the link) |
| `POST /sign/session/code` (M2) | session | Sends or checks an email code when `auth_method=link+email_code`; fails closed while Redis is down |
| `GET /sign/agreement` | session | Recipient-scoped view: title, sender name, message, own fields only, other recipients' progress (names only), routing position |
| `GET /sign/agreement/document` | session | Streams the snapshot PDF |
| `POST /sign/consent` | session + CSRF | Records the disclosure version and acceptance |
| `PUT /sign/fields/{field_id}` | session + CSRF | Only the recipient's own, not-yet-completed fields |
| `POST /sign/signature` | session + CSRF | Multipart: method and image, or typed text and font. Stored as a `signature` file (§7.3) |
| `POST /sign/complete` | session + CSRF, `Idempotency-Key` | Checks every required field; recipient → SIGNED or APPROVED; advances routing; may queue finalization |
| `POST /sign/decline` | session + CSRF | `{reason}`; agreement → DECLINED |
| `GET /sign/download` | session | Final PDF, only when COMPLETED and policy allows |

Every portal call resolves `token_hash → (workspace, agreement, recipient)` in the control plane,
then loads the data plane through `InfrastructureResolver`. It then re-checks that the recipient
belongs to the agreement and the agreement to the workspace. Every query is filtered by all
three ids.

## 5. State machines

### 5.1 Agreement

| From | Event | To | Guard |
|---|---|---|---|
| DRAFT | send | SENT | ≥ 1 signer; each signer has ≥ 1 signature field; every field inside its page; not expired; entitlements |
| SENT | first recipient completes (others remain) | PARTIALLY_SIGNED | |
| SENT, PARTIALLY_SIGNED | last required recipient completes | FINALIZING | Finalization queued |
| FINALIZING | finalize succeeded | COMPLETED | |
| FINALIZING | finalize failed after N attempts | FINALIZATION_FAILED | Recoverable: an operator re-queues it → FINALIZING |
| SENT, PARTIALLY_SIGNED | a recipient declines | DECLINED | |
| SENT, PARTIALLY_SIGNED | sender or admin voids | VOIDED | Reason required |
| SENT, PARTIALLY_SIGNED | `expires_at` passes (sweep) | EXPIRED | |
| DRAFT | delete | (row deleted) | Drafts only; events kept until the draft is deleted |

**FINALIZING is added to the prompt's list.** It's an internal state, "all signed, PDF being
built". It prevents voiding or new signatures in the window between the last signature and
completion. In the UI it reads "Completing…".

**Terminal states:** COMPLETED, DECLINED, VOIDED and EXPIRED accept no further events except
reads, downloads and verification.

**Void after partial signing:** signatures already given are kept as evidence. No final PDF is
produced. Every recipient is notified, all tokens are revoked, and events record who voided and
why.

### 5.2 Recipient

| From | Event | To |
|---|---|---|
| PENDING | their group becomes active (send, or the previous group completes) | INVITED (invitation queued) |
| INVITED | portal session opened | VIEWED |
| VIEWED | consent accepted | CONSENTED |
| CONSENTED | complete (signer) | SIGNED |
| CONSENTED | complete (approver) | APPROVED |
| INVITED, VIEWED, CONSENTED | decline | DECLINED |
| PENDING, INVITED, VIEWED, CONSENTED | agreement voided, or recipient removed before their turn | CANCELLED |
| PENDING, INVITED, VIEWED, CONSENTED | agreement expires | EXPIRED |

"Delivered" isn't a state. Email submission is recorded as an event
(`notification.submitted`); delivery is shown only if a provider reports it later.

### 5.3 Enforcement

- One function, `transition(agreement, event, actor)`, in `domain/signing`, unit-tested over the
  whole table.
- Services call it inside a transaction that holds `SELECT … FOR UPDATE` on the agreement row.
- No generic update endpoint touches `status`.
- Field values and signatures are write-once:
  - `agreement_fields.value` is set only while the recipient is CONSENTED and the field is
    empty;
  - `agreement_signatures.field_id` is unique.
- Routing: a recipient may act only when every recipient in lower `group_order` groups has
  completed. Sequential = one recipient per group; parallel = everyone in group 1.

## 6. PDF strategy

### 6.1 Libraries (decision record)

| Need | Choice | Licence | Why |
|---|---|---|---|
| Page rendering (editor and portal) | **pdfjs-dist 6.5.x** | Apache-2.0 | De facto standard; runs in the browser from the same origin under the current CSP (`isEvalSupported: false`, worker, fonts and cMaps served as static assets) |
| Open, inspect, flatten, overlay, save | **pikepdf 10.x** (qpdf) | MPL-2.0 (file-level copyleft; unmodified use from Python is fine) | Robust object model; reports encryption; flattens annotations and form fields; `Page.add_overlay`; reads MediaBox, CropBox and `/Rotate` |
| Draw signature appearances and certificate | **reportlab 5.x** | BSD | Mature PDF drawing; images and text with embedded fonts |
| Fallback | pypdf 7.x | BSD-3 | Pure Python; used only if a pikepdf gap appears |
| Future PAdES | pyHanko | MIT | Not in the initial release; the port leaves room |
| Existing text extraction | PyMuPDF (unchanged) | AGPL-3.0 or commercial | **Not extended by Sign.** Its licence is an existing product question, recorded in 03 §6 |

The pikepdf and reportlab wheels must be added to `uv.lock` and the runtime image. Image size
and the dependency audit will be checked in M1.

### 6.2 Snapshot (when the agreement is created)

1. Load the pinned source version's bytes from workspace storage. Only `application/pdf` is
   accepted.
2. Open with pikepdf:
   - **reject** any PDF with `/Encrypt`, including owner-password-only files (`unsupported_pdf`);
   - **detect** existing digital signatures (`/AcroForm /SigFlags` or fields with `/FT /Sig`).
     If present, require `confirm_invalidates_signatures=true` and record an event.
3. Generate appearance streams for form fields, then **flatten** annotations and form fields
   into the page content. Links stay; JavaScript is removed; OpenAction is removed.
4. Save as a new file and compute SHA-256. Store it at
   `workspaces/{ws}/agreements/{agreement}/snapshot.pdf` as an `agreement_files` row of kind
   `snapshot`.
5. Record `page_geometry`: for each page, `{index, mediabox, cropbox, rotate}`.
6. The **original source version is never modified**. Its hash is copied into the evidence
   (`source_sha256`).

### 6.3 Coordinates

**Stored values:**
- `x`, `y`, `width`, `height` in PDF points;
- in **displayed page space**: the CropBox after `/Rotate` is applied;
- origin at the top left, y pointing down;
- plus the page's `page_width`, `page_height` and `page_rotation` at the time the field was
  placed, which must match `page_geometry` when sending.

**Converting a displayed point to PDF user space.** The CropBox is `(cx0, cy0, cx1, cy1)` and the
displayed point is `(u, v)`.

| `/Rotate` | x | y | Displayed size |
|---|---|---|---|
| 0 | `cx0 + u` | `cy1 − v` | `cw × ch` |
| 90 | `cx0 + v` | `cy0 + u` | `ch × cw` |
| 180 | `cx1 − u` | `cy0 + v` | `cw × ch` |
| 270 | `cx1 − v` | `cy1 − u` | `ch × cw` |

- **Rectangles:** convert both corners and normalise.
- **Upright appearance:** the drawn content is rotated by the same angle, so it appears upright
  to the reader.
- **Browser side:** PDF.js renders at `scale`; the editor stores `pixel / scale` and never
  stores pixels.
- **Tests:** a unit-test matrix over the four rotations, odd CropBoxes and page sizes. An
  integration test renders the final PDF (PyMuPDF in tests only) and checks ink inside each
  field's rectangle.

### 6.4 Finalization (background job)

1. **Trigger:** the transaction that completes the last required recipient sets FINALIZING
   and, after commit, sends Celery `finalize_agreement(agreement_id, workspace_id)`.
2. **Claim:** the worker locks the agreement row (`FOR UPDATE SKIP LOCKED`), checks status
   FINALIZING and no `final` file, and sets `finalize_lease_until`.
3. **Build:**
   - check that every required field has a value;
   - render overlays with reportlab;
   - stamp them onto the snapshot with pikepdf;
   - flatten;
   - write `workspaces/{ws}/agreements/{id}/final-{attempt}.pdf`;
   - compute SHA-256.
4. **Certificate:** render the completion certificate (§9.2) and store it as
   `certificate-{attempt}.pdf` with its hash.
5. **Commit (one transaction):** insert the `final` and `certificate` file rows, create the
   signed DocuMind document (§11), append the events, set COMPLETED.
6. **Notify:** completion emails are queued through notifications.

**Exactly once:**
- the row lock plus the "no `final` file" check;
- the partial unique index on `(agreement_id, kind='final')`;
- files from attempts that lost or failed are unreferenced, and the orphan reconciler removes
  them after 24 h (agreement keys registered, §7.2).

**Retry:** failures increment `finalize_attempts`. After 5, the status becomes
FINALIZATION_FAILED. A sweep re-dispatches expired leases. Operators can run
`documind-manage agreements finalize <id>` or use the API.

## 7. Storage

### 7.1 Keys

All under `workspaces/{ws}/agreements/{agreement}/`:
- `snapshot.pdf`;
- `final-{n}.pdf`;
- `certificate-{n}.pdf`;
- `signatures/{signature_id}.png`.

Workspace storage comes from the resolver, so customer buckets are respected. Files are never
overwritten.

### 7.2 Integration (M1, before the first write)

| Component | Change |
|---|---|
| `ops/orphans.py` | Keys under `workspaces/{ws}/agreements/` are referenced if an `agreement_files` row has them |
| `migration.py` | `_object_keys` includes `agreement_files`; `_verify` and `cleanup_source` too |
| Backups | No change (whole bucket and full dumps); covered by a restore test |
| Data export and deletion | Per decision C1 |

### 7.3 Signature images

- Drawn signatures: PNG sent by the browser. Uploads: PNG or JPEG.
- **Server-side cleaning:** decode with Pillow, check the real format, limit to 1 MB and
  2000 × 1000 px, strip metadata, re-encode as PNG.
- **Typed signatures:** rendered on the server with a bundled, licence-checked script font. The
  text and font name are stored.
- Never in database rows; never logged.

## 8. Authorization

### 8.1 Permissions

New permissions, using the existing `_own` / `_any` pattern:

| Permission | Owner | Admin | Member | Viewer |
|---|---|---|---|---|
| `agreements.read` | ✓ | ✓ | ✓ | ✓ |
| `agreements.create` (create, edit own drafts, send, resend own) | ✓ | ✓ | ✓ | |
| `agreements.void_own` | ✓ | ✓ | ✓ | |
| `agreements.void_any` (also re-queue finalization) | ✓ | ✓ | | |
| `agreements.audit_own` | ✓ | ✓ | ✓ | |
| `agreements.audit_any` | ✓ | ✓ | | |
| `signing_templates.manage` (M3) | ✓ | ✓ | | |

**Which agreements a reader sees (R11, approved):**
- Owners and admins: every agreement in the workspace.
- Members and viewers: agreements they created, plus agreements where they are a recipient by
  the C3 rule.
- No one can see an agreement whose source document is hidden from them by a restricted
  collection, unless they created it or are a recipient.

### 8.2 Open question for the M0 review

The prompt grants members read access to "all authorized" agreements. This design reads that as
the rule above. The alternative, "members see every agreement in the workspace", would expose
other members' contracts.

## 9. Evidence

### 9.1 Agreement events (append-only)

**Event types:**
- `agreement.created`, `snapshot.created` (hashes), `recipients.set`, `fields.set`, `sent`;
- `notification.submitted` / `notification.failed`;
- `portal.opened`, `disclosure.shown`, `consent.accepted`;
- `field.completed`, `signature.added`, `recipient.completed`, `recipient.declined`;
- `agreement.voided`, `agreement.expired`;
- `finalization.started` / `finalization.failed`, `final.generated` (hash),
  `certificate.generated` (hash), `agreement.completed`;
- `file.downloaded`, `verification.run`.

**Integrity:**
- **Hash chain:** `hash = SHA-256(prev_hash ‖ canonical JSON of the event)`. Verification walks
  the chain. This is tamper-*evident*, not tamper-proof, and the documentation says so.
- **Database protection:** a trigger in both Alembic environments raises an error on `UPDATE`
  of `agreement_events`. `DELETE` remains possible only through the workspace and organization
  deletion path decided in C1. No API edits or deletes events.
- **Workspace audit log:** workspace-level actions (sent, voided, completed, downloaded) are also
  written through `AuditRecorder`, so they appear in the existing audit log and outbox relay.

**IP address and user agent:** recorded for portal actions (open, consent, sign, decline) as
signing evidence. The legal basis and retention are covered in 03 §4.

### 9.2 Completion certificate (PDF, generated by DocuMind)

- **Contents:**
  - agreement title and id;
  - source, snapshot and final SHA-256;
  - sender (name and email);
  - each recipient: name, email, role, order, auth method, consent time, action and time, IP
    address;
  - the signing order;
  - completion time and generation time;
  - the event-chain head hash.
- **Footer:** "Generated by DocuMind. This is an evidence summary, not a certificate issued by
  a government or trust-service provider."

## 10. Email

- **Templates** (functions, in the `emails.py` style): invitation, reminder, completed,
  declined, voided, expiring soon, expired. Every email has text and HTML versions.
- **Contents:** "{Sender name} at {Workspace} sent you {title}"; the action; the expiry; a link
  `https://app/sign#<token>` (token in the **fragment**, so it never reaches server logs or the
  Referer header). No attachments.
- **Sending:**
  - a service inserts an `agreement_notifications` row with
    `idempotency_key = sha256(agreement_id:recipient_id:kind:sequence)` in the same
    transaction as the state change;
  - a Celery task sends pending rows and marks them sent;
  - a sweep retries failed rows with backoff, up to 5 attempts, then records
    `notification.failed`.
- **Duplicates:** a duplicate task finds the row already `sent` and does nothing.
- **Reminders (M2):** a beat task every hour; `reminders_sent` on the recipient; configurable
  interval and maximum.
- **Expiry:** the same sweep handles "expiring soon" and expiry.
- **Delivery:** shown as "submitted to the mail server". Inbox delivery is not claimed.

## 11. DocuMind integration

### 11.1 Before sending

- **Source document:** the source is always a DocuMind document. Uploading through Sign uses
  the normal upload pipeline (validation, ClamAV, processing, indexing). Visibility is per C4.
- **Duplicates:** if the uploaded PDF already exists, the existing document is reused when the
  sender can see it; otherwise the response is a generic duplicate error, as today.
- **Questions:** "Ask about this contract" uses the existing `/ask` with
  `document_ids=[source]`, with citations.
- **M3 suggestions:**
  - parties, signers, roles and emails (only when present in the text);
  - suggested fields from word positions read from the **snapshot**. pikepdf doesn't extract
    text positions, so this needs either pdfminer.six (MIT) or PyMuPDF, which is already used
    but has the licence question in 03 §6. The choice is made in M3.
  - Every suggestion is shown with its source and confidence, and needs explicit acceptance.

### 11.2 After completion

- **Signed document:** the final PDF becomes a new document, "{title} (signed)". It is linked
  through `agreements.signed_document_id`. Its version's `storage_key` is the agreement's final
  file key, which isn't duplicated.
- **Placement:** per C4 (§15): the same restricted collections as the source, otherwise the
  agreement's restricted collection.
- **Processing:** it goes through normal processing (indexing, deadline extraction), so
  questions cite the signed version.

## 12. Processing modes and customer infrastructure

| Workspace | M1 behaviour |
|---|---|
| Shared database and DocuMind storage | Supported |
| Dedicated or external database, any storage | Supported: data-plane tables migrate with `documind-manage dataplanes migrate`; files go to the workspace's storage |
| Processing mode `hybrid` | Supported (DocuMind's workers finalize) |
| Processing mode `customer` | **Sign unavailable** (`409 sign_requires_documind_processing`), because finalizing would process customer files on DocuMind workers. A future customer-worker task type can lift this |
| Workspace being migrated | Portal and API answer 503 `migration_in_progress`, as today. Tokens stay valid |

## 13. Security

| Threat | Control |
|---|---|
| Token theft or leakage | 256-bit tokens; SHA-256 at rest; token in the URL fragment; exchanged for an HttpOnly, `SameSite=Strict` cookie scoped to `/api/v1/sign`; redacted in logs (`dms_` prefix added to the redaction patterns) |
| Brute force | Rate limits per IP and per token hash on `/sign/session` and `/sign/session/code`; code checks fail closed while Redis is down; codes hashed with HMAC and the MFA pepper, 10-minute expiry, 5 attempts |
| IDOR | Every portal query filters by `(workspace, agreement, recipient)` from the token; workspace routes are covered by the existing sweep; a new `test_signing_isolation` covers portal cases (prompt §10.4) |
| CSRF | Portal mutations need the existing CSRF header plus the SameSite cookie |
| XSS | Recipient-supplied text is never rendered as HTML; emails escape every value |
| Malicious PDFs | Existing upload validation and ClamAV; pikepdf in the worker, with page-count and size limits; JavaScript and actions removed in the snapshot |
| Malicious images | Pillow decode and re-encode, with size and pixel limits |
| SSRF | No URL fetching anywhere in Sign |
| Email enumeration | Portal errors are generic (`invalid_or_expired_link`); resend is rate-limited |
| Session fixation | A new session token on every exchange; revoked on completion, decline or void |
| Sensitive logging | No tokens, signature data or field values in logs; extend the redaction tests |
| Overwriting evidence | Write-once fields; unique signature per field; UPDATE-blocking trigger; hash chain |

## 14. Failure and recovery

| Failure | Behaviour |
|---|---|
| SMTP down at send | The agreement is SENT and the notification row is pending. The sweep retries; the sender sees "not yet submitted" |
| Worker dies mid-finalization | The lease expires; the sweep re-dispatches; the row lock and unique final file prevent duplicates |
| Finalization fails repeatedly | FINALIZATION_FAILED; an operator re-queues it; files from attempts are cleaned by the orphan reconciler |
| Storage unavailable during signing | Signature upload fails with 503; nothing is recorded; the recipient retries |
| Data plane unavailable | 503 `data_plane_unavailable` for that workspace only, as today |
| Two parallel signers finish together | Both commits take the agreement row lock in turn; only the second sees "all complete" and queues finalization |
| Redis down | Session checks continue (database-backed); code checks are refused |

## 15. Decisions (approved 2026-11-28)

| Item | Decision |
|---|---|
| C1 | Define a configurable retention policy after legal review; do not silently retain deleted customer data |
| C2 | Pro and above initially, with configurable limits |
| C3 | Authenticated DocuMind users sign in the app; external recipients sign through secure email links |
| C4 | Inherit source collection restrictions; otherwise restrict to agreement participants |

### C1. Deletion and retention

**Policy surface (configuration, not hard-coded):**
- **Workspace retention setting** `signing.retention`: what happens to **completed, declined,
  voided and expired** agreements while the workspace exists.
  - `keep` (default): kept until the workspace is deleted.
  - `delete_after_days: N`: a daily sweep deletes the agreement, its files and its events, and
    leaves an audit entry with the agreement id and file hashes only.
  - Owners change it in workspace settings, with 2FA step-up.
- **Platform setting** `SIGNING_RETAIN_AFTER_WORKSPACE_DELETION_DAYS`: **fixed at 0** (delete
  with the workspace) until legal review defines a lawful retention basis.
  - Any other value is **refused at startup** unless it comes with
    `SIGNING_RETENTION_LEGAL_BASIS_REF`, a reference to the approved legal decision.
  - The mechanism for keeping evidence after deletion is **not built** until that review: a
    separate store with its own access and erasure process. It is a tracked follow-up.

**Behaviour now (equivalent to setting 0):**
- **Organization deletion:**
  - in-progress agreements are voided first: recipients are notified and tokens revoked;
  - then everything is deleted with the workspace, as ADR 0039 does for documents;
  - before the confirmation, the owner is shown how many completed agreements exist, with a
    one-click export (final PDF, certificate, events JSON).
- **Account deletion:** agreements stay with the organization; `created_by_user_id` becomes
  empty. In-progress agreements that the person **sent from their personal workspace** are
  voided and deleted with it.
- **Nothing is retained silently.** The deletion confirmation lists exactly what will be
  deleted.
- **Data export:** a person's export lists agreements they sent, and agreements they signed
  in the app (C3), with status and hashes. Files are copied only from their personal workspace.

### C2. Plans and limits

- **Availability:** Pro, Business and Enterprise (including the Pro trial). Free sees an upgrade
  prompt, as for integrations.
- **Mechanism:** new `Entitlements` fields, so limits are configurable per plan and overridable
  per workspace with `documind-manage plan assign --override`:
  - `signing: bool`;
  - `agreements_per_month: int | None`;
  - `recipients_per_agreement: int | None`.
- **Proposed initial values** (configuration; no pricing or Stripe changes):

| Plan | `signing` | Agreements per month | Recipients per agreement |
|---|---|---|---|
| Free | no | 0 | 0 |
| Pro (and trial) | yes | 50 | 10 |
| Business | yes | 500 | 25 |
| Enterprise | yes | unlimited | 50 |

- **Counting:** sending is what counts (DRAFT → SENT), not drafts. Usage appears on the Usage
  page. Over the limit: `402 quota_exceeded`, the existing response format.

### C3. Internal and external recipients

- **External recipients:** the secure email link → portal session (§4.2, §13).
- **Authenticated DocuMind users:**
  - **Who:** a recipient is "internal" when their email matches the **verified** email of a
    member of the agreement's workspace. The match is made at send time and stored as
    `agreement_recipients.user_id`.
  - **Where they see it:** "Waiting for me" lists it. "Sign" opens the same portal UI inside the
    app.
  - **How the session starts:** `POST /workspaces/{ws}/agreements/{id}/signing-session`, which
    requires their logged-in session, the workspace's 2FA rule, and a recent 2FA step-up if the
    organization requires 2FA. The evidence records `auth_method = documind_session` (plus
    `mfa` when used).
  - They **also** receive the email link and may use either. The first completed action wins;
    the other path then shows "already signed".
  - **Membership changes:** if the person leaves the workspace before signing, in-app signing
    stops working; the email link still works.
- **Same rules for both paths:** the same state machine, consent step, evidence and routing
  order.
- **Milestone:** in-app signing moves into **M1**, since it's part of the approved scope and
  shares the portal code. It remains a separate, small endpoint.

### C4. Visibility of the signed PDF

**Rule:**
- **Source document in one or more restricted collections:** the signed PDF is added to
  **the same** restricted collections, so it inherits exactly the source's restrictions.
- **Otherwise** (source in no restricted collection, or uploaded through Sign without choosing
  a restricted collection): the signed PDF goes into a new **restricted collection for the
  agreement**, "Agreement: {title}".
  - **Members:** the sender and every internal recipient (C3).
  - **Also:** owners and admins, who see every document under ADR 0036.
  - **External recipients** see the signed PDF only through their portal download, never in
    the workspace.
- **Source documents uploaded through Sign** follow the same rule: the agreement's restricted
  collection, unless the sender picks a restricted collection at upload.
- **Never** added automatically to a broader collection. Later changes to the source's
  collections don't change the signed copy; a change to the signed copy is a deliberate admin
  action.
- **Mechanism:** reuses the existing visibility SQL (ADR 0036); no new access path. The
  agreement collections are marked `kind = 'agreement'` (a new column with default
  `'standard'`), so the Collections page can group or hide them. Only admins and the sender can
  edit their membership.

**Consequence:** a signed copy can be **narrower** than its unrestricted source. That's
intended: the decision says "otherwise restrict to agreement participants".

### R11. Who sees which agreements (approved)

Owners and admins see every agreement in the workspace. Members and viewers see only the
agreements they created or are a recipient of (§8.1).

## 16. Testing strategy

| Level | What | Notes |
|---|---|---|
| Unit | Transition table (every pair); routing; coordinate maths (4 rotations × CropBox offsets); field validation; idempotency keys; hash chain; image cleaning | Pure domain |
| Integration (real PostgreSQL, Redis, MinIO, Mailpit) | Full lifecycle: sequential, parallel, groups; decline, void, expire; finalization concurrency (two completes at once, a duplicate task, a killed worker); encrypted, signed and form PDFs; rotated multi-page PDFs (ink-in-box check); token invalid, expired, revoked, reused, cross-recipient, cross-agreement, cross-workspace; brute force; Redis down; orphans and migration include agreement files; dedicated and external data planes (existing fixtures); C1 deletion | Extends existing fixtures and the tenant sweep |
| Contract | OpenAPI and TypeScript types | Existing test |
| E2E (Playwright, dev stack) | Prompt §17.7: admin creates, three external signers sign sequentially through Mailpit links, final PDF, certificate, search, AI question cited to the signed document; portal on a phone viewport; axe in light and dark | Mailpit API for links |
| Production smoke stack | One agreement end to end over HTTPS | Phase gate for M4 |

Results go in `04-test-results.md`, recording only what was actually run.

## 17. Deployment implications

- **Dependencies:** `pikepdf`, `reportlab` and `Pillow` (all three new; Pillow isn't in
  `uv.lock` today) in `uv.lock` and the runtime image; `pdfjs-dist` in the frontend, with its worker, fonts and cMaps copied
  to static assets. The CSP is expected to be unchanged; this will be verified on the smoke
  stack.
- **Migrations:** control 0024 (permissions, `signing_tokens`, plan entitlements); data plane
  dp0006 (agreement tables and trigger), applied to dedicated planes with
  `documind-manage dataplanes migrate`.
- **Settings:** `SIGNING_LINK_TTL_DAYS` (default 30), `SIGNING_SESSION_MINUTES` (120),
  `SIGNING_MAX_PDF_BYTES`, `SIGNING_MAX_PAGES`, `SIGNING_REMINDER_*`, `SIGNING_MAX_RECIPIENTS`
  (hard cap).
- **Beat tasks:** notification sweep, finalization sweep, expiry and reminder sweep.
- **Runbook:** stuck finalization, failed notifications, verification failure.

## 18. Milestones

| Milestone | Content | Ends with |
|---|---|---|
| M0 | This document, 01 and 03, decisions | Your approval |
| M1 | Tables and migrations (both environments), permissions, `signing_tokens`, orphan, migration, export and deletion integration (C1 behaviour now), transition service, drafts and recipients API, snapshot (pikepdf), simple field editor (PDF.js, place and move), send, notifications outbox, portal (consent, fields, drawn and typed signatures, complete and decline), sequential and parallel routing, finalization, certificate, evidence and verify, signed document per C4, in-app signing for internal recipients (C3), plan gating (C2), dashboard and detail pages | Demo and test results |
| M2 | Editor polish (resize, rotated-page tests in the browser), groups, reminders, expiry, email codes, uploaded signatures, workspace retention setting (C1), void UI, completed-agreement page | Demo and test results |
| M3 | AI: party and signer suggestions, field suggestions, contract questions in the wizard, deadline extraction on the signed copy, templates | Demo and test results |
| M4 | §17.7 end-to-end scenario, security review, smoke stack, user, operations and deployment documentation, `04-test-results.md` | Release review |
