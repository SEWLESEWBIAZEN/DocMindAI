# ADR 0007: Frontend toolchain (Vite, TypeScript 7, oxlint, Vitest)

- Status: Accepted
- Date: 2026-11-23

## Context

The specification requires React, Vite and TypeScript, plus TanStack Query,
Axios, Vitest, React Testing Library and Playwright (Phase 9).

## Decision

- Scaffold from the current official `create-vite` React + TypeScript
  template and keep its defaults: **TypeScript 7** (native compiler),
  **oxlint** for linting, and `tsc -b` for type checking.
- **Vitest + jsdom + React Testing Library + jest-dom** for unit and component tests.
- **Feature-based structure:** `src/features/<feature>/{api,types,hooks,components}`,
  shared code in `src/lib`, app wiring in `src/app`.
- One Axios instance (`src/lib/api/client.ts`) with interceptors that turn
  every failure into a typed `ApiError`. TanStack Query manages server state.
- The browser calls same-origin `/api`. The Vite dev server proxies it, and
  Nginx will proxy it in production, so no CORS is needed in standard deployments.
- No CSS framework yet. Plain CSS with design tokens as custom properties
  (light and dark). Revisit the component library choice at the start of Phase 7.

## Alternatives

- *ESLint + typescript-eslint:* more plugins (e.g. jsx-a11y), but slower. The
  template's oxlint is sufficient now. Accessibility linting will be reassessed
  in Phase 7.
- *Next.js:* SSR is not needed for an authenticated SPA, and it would add a
  Node server to operate.

## Consequences

- TypeScript 7 is recent. If a needed tool lacks support, pin `typescript@5.x`
  for that tool.
- API types are hand-written mirrors of backend schemas for now. Generating
  them from OpenAPI will be evaluated when the API surface grows (Phase 2/7).
