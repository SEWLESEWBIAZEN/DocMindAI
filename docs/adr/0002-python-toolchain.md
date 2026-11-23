# ADR 0002: Python toolchain (Python 3.12, uv, pyproject + uv.lock)

- Status: Accepted
- Date: 2026-11-23

## Context

The specification requires `pyproject.toml` with a lockfile. Builds must be
reproducible in CI and Docker, and fast enough for frequent rebuilds.

## Decision

- **Python 3.12** (`requires-python = ">=3.12,<3.14"`). It is mature, has
  broad wheel availability (asyncpg, PyMuPDF, uvloop), and its security support
  covers the planned lifetime.
- **uv** for dependency management. `uv.lock` is committed. Installs in CI and
  Docker use `--frozen`, so the lockfile is never silently re-resolved.
- Dev tooling: **Ruff** (lint and format), **mypy --strict** with the pydantic
  plugin over `src` and `tests`, **pytest** with **pytest-asyncio**.
- The Docker `runtime` stage copies only the virtualenv built by uv. uv itself
  is not in the production image.

## Alternatives

- *Poetry:* mature, but slower, and its lockfile and build backend are less
  standard. uv uses standard `pyproject` metadata and PEP 735 dependency groups.
- *pip-tools:* workable, but handles virtualenvs and Python versions only
  indirectly.

## Consequences

- Contributors need uv ≥ 0.11 (`make setup`).
- Changing a dependency requires `uv lock`. CI fails if `uv.lock` is stale.
