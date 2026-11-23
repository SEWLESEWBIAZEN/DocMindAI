# ADR 0010: Application services use concrete repositories (amends ADR 0001)

- Status: Accepted
- Date: 2026-11-23

## Context

ADR 0001 says the application layer depends only on the domain layer and on
ports. Phase 2 introduced the first real persistence. A pure approach would
need repository Protocols plus mapping between ORM rows and separate domain
entities for every aggregate.

## Decision

- Ports (`application/ports.py`) remain for **external systems**: password
  hashing, token signing, email, rate limiting, and later storage, LLM and
  embeddings. These are the integrations most likely to be swapped or faked.
- For **persistence**, application services use the concrete SQLAlchemy
  repositories in `infrastructure/database/repositories`, and treat ORM models
  as persistence entities. Services never build queries themselves. Every query
  lives in a repository, where tenant filters are enforced.
- **Business rules** that don't need I/O (permissions, hierarchy, ownership,
  value validation) stay in `domain` as pure functions and are unit-tested in
  isolation.

## Alternatives

- *Full repository Protocols + domain entities + mappers:* roughly doubles the
  persistence code for a benefit we don't need. We have one database and test
  against the real one.

## Consequences

- Service tests that exercise persistence are integration tests against real
  PostgreSQL, which also covers the SQL tenant filters. Pure rules keep fast
  unit tests.
- If a second persistence backend is ever needed, introduce Protocols at that
  point. The repository boundary already isolates the queries.
