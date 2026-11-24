# ADR 0017: Frontend application architecture

- Status: Accepted
- Date: 2026-11-24
- Extends: ADR 0007 (toolchain), ADR 0011 (accessibility)

## Context

Phase 7 delivers the complete product UI. It covers 16 pages, plus the email-link
pages (verify, reset, accept an invitation). Users must be able to complete
every journey without Swagger or manual API calls. The playbook asks for
feature-based code, TanStack Query, Axios interceptors, typed contracts,
protected routes, error boundaries, skeletons, toasts, and accessible,
responsive layouts in the style of Linear, Notion and ChatGPT.

## Decision

### Structure

```
src/app/              providers, router (lazy routes), error boundary, 404
src/components/ui/    Button, Field, Dialog + useConfirm, Toast, Menu, states, Pagination…
src/lib/api/          client (Axios), session (token + refresh), sse, errors, generated types
src/lib/workspace/    WorkspaceContext, role → permission mirror
src/features/<name>/  auth, shell, dashboard, documents, chat, admin, landing, system-status
```

### Libraries added

| Need | Choice | Why |
|---|---|---|
| Routing | `react-router` 7 (data router) | Nested layouts, protected routes, optional segments, lazy pages. |
| Markdown | `react-markdown` + `remark-gfm` | Safe by default: raw HTML is never rendered (`skipHtml`). We additionally allow only http(s)/mailto links, drop images, and turn `[n]` into citation buttons. |
| 2FA QR code | `qrcode` | Generated locally from the `otpauth://` URI, so the secret never leaves the browser. The setup key is also shown as text, for screen readers and manual entry. |
| E2E | `@playwright/test` (pinned to the installed browser build) + `@axe-core/playwright` | Real-stack journeys with WCAG scans. |

There is no component library and no icon dependency. The UI kit is small,
token-based CSS (`styles/app.css`) built on the native `<dialog>` element and
WAI-ARIA patterns.

### Typed API contract

`frontend/src/lib/api/openapi.json` is generated from the backend app.
`schema.d.ts` is generated from it with `openapi-typescript` (`make
api-types`). A backend unit test fails when the committed contract is stale,
so frontend types cannot silently drift from the API.

### Sessions

- The access token lives **in memory only**. The refresh token is the existing
  HttpOnly, `SameSite=Strict` cookie scoped to `/api/v1/auth`.
- On load, a silent `POST /auth/refresh` restores the session.
- Axios interceptors attach the token and the CSRF header. On a 401, one
  **shared** refresh request runs and the original request retries once.
  Requests that carry their own token (the 2FA step-up for deletion) are never
  refreshed.
- When a refresh fails, the session is marked expired, the query cache is
  cleared, and the next protected route redirects to `/login?next=…`. Redirect
  targets are restricted to same-origin paths, so there are no open redirects.

### Workspaces and permissions

All app routes live under `/w/:workspaceId/…`. A workspace id the user doesn't
belong to renders the 404 page. The workspace switcher also updates the
default workspace on the server. The UI mirrors the backend role → permission
map to hide actions a role can't take; **the API remains the only
enforcement**.

### Chat streaming

The browser uses `fetch` with a small SSE parser, because `EventSource` can't
POST or send headers.

- The chat route is a single optional-segment route (`chat/:conversationId?`).
  A new conversation is created on the first question and the URL updates
  without remounting, so the stream isn't interrupted.
- Stop calls the cancel endpoint. Leaving the page aborts the request, which
  the backend records as `client_disconnected` (ADR 0016).
- The final `answer.completed` message replaces the streamed text, so a
  citation marker removed during verification disappears.

### Source navigation

A citation card links to
`/documents/:id?page=N&q=<excerpt>`. The viewer scrolls to the page, focuses it
and highlights the excerpt; the match tolerates whitespace and case
differences.

### Accessibility (ADR 0011)

- Skip link, and focus moved to each page's `<h1>` on navigation.
- Labelled landmarks.
- Forms: `<label>` elements, with hints and errors linked through
  `aria-describedby`.
- Menus (arrow keys, Home/End, Escape) and dialogs (native modal focus trap)
  follow WAI-ARIA patterns.
- Toasts use live regions: polite for success, assertive for errors.
- Streamed answers are marked `aria-busy` while they stream, and a polite
  "Answer complete with N sources" announcement follows. Token-by-token
  chatter is not announced.
- Citations have spoken labels ("Open vendor policy, page 14").
- The usage chart has a text summary and a table alternative.
- Reduced motion is respected.
- Colour tokens meet WCAG AA, verified by axe on every major page in the
  end-to-end run.

## Alternatives considered

- **Storing the access token in `localStorage`.** Rejected: it would be
  readable by any XSS.
- **A component library (MUI, Chakra) or Tailwind.** Rejected: heavier, with a
  generic look. The token-based kit stays small and matches the landing page.
- **`EventSource`.** Rejected: GET only, and it can't send an `Authorization`
  header.
- **A hand-written Markdown renderer.** Rejected: subtle parsing and XSS bugs;
  react-markdown is mature and safe by default.

## Consequences

- Route chunks are lazy-loaded; the landing page doesn't download the app.
- The UI permission mirror must be kept in sync with
  `domain/identity/permissions.py`. A mismatch only affects what's shown, not
  what's allowed.
- End-to-end runs use real sign-ups, so the backend's auth rate limits apply.
  Repeated local runs need those limits raised or the Redis `rl:*` keys
  cleared.
