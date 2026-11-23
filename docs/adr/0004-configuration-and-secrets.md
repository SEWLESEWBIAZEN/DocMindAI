# ADR 0004: Environment-based configuration with fail-fast validation

- Status: Accepted
- Date: 2026-11-23

## Context

No secrets may be committed. The same image must run in development, CI and
production. Misconfiguration (e.g. shipping development credentials) must be
caught before the service accepts traffic.

## Decision

- A single typed `Settings` class (pydantic-settings) is the only place that
  reads configuration. Secrets are `SecretStr`, so they are never rendered in
  reprs or logs.
- **Required secrets have no defaults.** URL schemes, bucket names and numeric
  ranges are validated at startup.
- In `production`, startup fails if any secret contains a development
  placeholder marker or CORS allows `*`. OpenAPI docs default to off.
- `.env` is git-ignored. `.env.example` holds clearly marked development
  placeholders. Compose uses `${VAR:?}` so a missing `.env` fails loudly.
- Unit tests construct `Settings` explicitly with `_env_file=None` and never
  depend on a developer's `.env`.

## Alternatives

- *Committed per-environment YAML:* invites committing secrets and splits
  configuration across multiple sources.
- *Secrets manager integration now:* premature for a single VPS. Environment
  variables (or Docker secrets mapped to them) keep that path open.

## Consequences

- In development, the root `.env` duplicates credentials between the
  infrastructure variables and the backend URLs. This is documented in
  `.env.example`.
- The full variable reference lives in `docs/CONFIGURATION.md` and must be
  updated with every new setting.
