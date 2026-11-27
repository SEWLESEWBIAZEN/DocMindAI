# 04 — Operational verification

- Date: 2026-11-27
- Environment: the production stack definition (`deploy/production/docker-compose.yml`) in
  production mode, through the smoke harness (`deploy/production/smoke/run.sh`):
  - Nginx with TLS on `https://localhost:8443`, using certificates from a local CA;
  - API with 3 Uvicorn workers, Celery worker and beat;
  - bundled PostgreSQL 16 + pgvector, Redis 7.4, MinIO and Mailpit;
  - a local OpenAI-compatible stub.
- Images:
  - Docker Hub rate-limits base-image pulls here, so the app runs the current source mounted over
    the last built runtime image (`SMOKE_USE_SOURCE=1`), which has the same locked dependencies.
  - The backup image was built from a drill-only Dockerfile, because `apt.postgresql.org` is
    blocked by this environment's network policy. That Dockerfile copies the PostgreSQL 16.16
    client tools from the local `pgvector/pgvector:0.8.7-pg16` image and runs the same scripts,
    user and entrypoint.
  - Rebuilding the real images is a Phase 7 item.
- CI/CD is out of scope.

## 1. Summary

| Area | Initial status | Final status | Evidence |
|---|---|---|---|
| Liveness and readiness | VERIFIED (dev) | VERIFIED (production stack) | section 2 |
| Metrics: protected, complete | CONFIGURED | VERIFIED | section 3 |
| Metrics during a Redis outage | BROKEN: 500 | VERIFIED (fixed) | D5-1; `test_metrics_keep_answering_and_report_recovery` |
| Rate-limiter outage signal clears after recovery | BROKEN: stayed 1 | VERIFIED (fixed) | D5-2; two probe unit tests |
| Scheduled backup on the production stack | BROKEN: failed on every run | VERIFIED (fixed) | D5-3, D5-4; section 4 |
| Restore onto clean volumes (DR: total loss) | VERIFIED in an integration test only | VERIFIED, timed drill | section 5 |
| Backup metric for the stale-backup alert | CONFIGURED | VERIFIED | section 4 |
| Start-up AI model check on the production stack | not present | VERIFIED | section 3 |
| TLS | VERIFIED | VERIFIED (re-run) | 22/22 `tls-check.sh` |
| End-to-end journey over HTTPS | VERIFIED | VERIFIED (re-run) | 5/5 Playwright |
| Offsite backup copy | CONFIGURED | CONFIGURED: not exercised | no offsite bucket here |
| Prometheus evaluating the alert rules | CONFIGURED | CONFIGURED | rules parse and every runbook link resolves (`test_alert_runbooks.py`); no Prometheus server or `promtool` here |

## 2. Health

| Check | Expected | Actual |
|---|---|---|
| `GET /api/v1/health/live` over HTTPS | 200 `{"status":"ok"}`, touches no dependency | 200 `{"status":"ok"}` |
| `GET /api/v1/health` | 200, one entry per dependency | 200: database 4.6 ms, Redis 0.7 ms, object storage ok |
| Readiness with Redis stopped | 503; liveness still 200 | 503 / 200 |
| Readiness after Redis restarts | back to 200 | 200, 17 s after the stop (including the restart) |
| `documind-manage check` on a restored stack | every component ok | ok |

## 3. Observability

**Metrics:**
- `/api/v1/metrics` answers 404 through Nginx from outside, and 401 without the bearer token
  inside the network.
- With the token it returns 14 metric families, including:

| Metric | Value seen | Meaning |
|---|---|---|
| `documind_up` | 1 | serving |
| `documind_dependency_up{component=database\|redis\|object_storage}` | 1 / 1 / 1; Redis 0 during the drill | readiness per dependency |
| `documind_rate_limiter_degraded` | 0; 1 during the outage, in the processes that saw sign-ins | F1 fallback active |
| `documind_queue_depth` | 0; omitted while Redis is down | processing backlog |
| `documind_backup_last_success_timestamp` | 0, then 241 s old after the drill backup | stale-backup alert |
| `documind_tls_certificate_expiry_timestamp`, `_self_signed` | expiry 2027-02-24; 0 | TLS alerts |
| `documind_ai_model_available{purpose,model}` | 1 for answers and embeddings | F10 start-up check |

**Logs:**
- JSON, one object per line, with `request_id`, method, path, status and duration.
- The path is logged, never the query string.
- Every response carries an `X-Request-ID` header.
- Redaction is covered by unit tests (doc 03, P4-2).

**AI model check on this stack:**
- The first start reported both models `missing`. Cause: the local OpenAI stub didn't implement
  `GET /v1/models/{id}`. It does now.
- Afterwards `documind-manage ai check` printed `ok` for both models, and the metric read 1.
- So the check and the alert work; the stub was simply incomplete.

## 4. Backup

Run as production runs it: the compose `backup` service, one-off mode.

```
docker compose ... --profile backup run --rm -e BACKUP_ONCE=true backup
```

The first three attempts each failed, in turn, on the defects below.

| # | Defect | Effect in production | Fix |
|---|---|---|---|
| D5-3 | `backup.sh` required `PGURL`, a second copy of `DATABASE_URL` in libpq form. An `.env` without it, or with a stale password after a rotation, failed every run | Nightly backups fail until `DocuMindBackupStale` fires after 26 h | `backup.sh` and `restore.sh` derive it from `DATABASE_URL` when unset (removing `+asyncpg`, and turning `ssl=` into `sslmode=`). `PGURL` is optional in `.env.example` |
| D5-4 | The `backup` image runs as `documind`, but `/backups` didn't exist in it, so Docker created the volume's mount point as root | `Permission denied` writing the archive: **every** backup failed | The `backup` stage in `backend/Dockerfile` creates `/backups` owned by `documind`, mode 0700 |
| — | The smoke override didn't mount the current source into `backup` (smoke harness only) | none | `backup` added to `smoke/docker-compose.source.yml` |

**Result after the fixes:**

| Step | Result |
|---|---|
| Data at backup time | 11 users, 15 workspaces, 1 processed document with 1 chunk, 10 messages, 69 audit rows |
| `pg_dump`, object export, checksums, encryption | succeeded |
| Archive | `documind-<UTC stamp>.tar.dmbk`, 204 KB, mode 0600, in a 0700 directory owned by `documind` |
| Duration | 17.6 s, including container start-up |
| `documind_backup_last_success_timestamp` | updated: the stale-backup alert clears |
| Offsite copy | not exercised: no offsite bucket in this environment |

`test_backup_restore.py` still passes after the script changes.

## 5. Restore drill (DR: total loss of the stack)

**Setup:**
- The target is a **separate** compose project, `documind-restore`, with new, empty PostgreSQL,
  MinIO and Redis volumes on its own network. No volume of that project existed beforehand.
- It uses the same `.env` and secrets, meaning the original `JWT_SECRET_KEY`,
  `MFA_ENCRYPTION_KEY` and `CREDENTIAL_ENCRYPTION_KEYS`, as the runbook requires.

| t (s) | Step |
|---|---|
| 0.0 | start empty PostgreSQL, MinIO and Redis |
| 14.2 | `bootstrap-storage`, then `restore.sh <archive>`: decrypt, verify checksums, refuse if not empty, `pg_restore`, objects import |
| 32.3 | `alembic upgrade head` (migrate service) |
| 45.8 | start API and worker |
| **58.6** | **API healthy** |
| ~70 | functional checks done |

**Verification against the restored stack:**

| Check | Source | Restored |
|---|---|---|
| Row counts: users, workspaces, documents, chunks, messages, audit | 11 / 15 / 1 / 1 / 10 / 69 | 11 / 15 / 1 / 1 / 10 / 69 |
| Sign in with the original password | ok | ok |
| Document status | ready | ready |
| Download through a presigned link | 88 bytes, marker text present | 88 bytes, marker text present |
| Hybrid search for the document's content | 1 hit | 1 hit |
| `documind-manage check` | ok | ok |

**Notes:**
- The first search attempt on the restored stack timed out. The drill started the API with
  `--no-deps`, so the AI stub wasn't running, and search needs the query embedding. Started on its
  own, search worked. In a real recovery the AI provider is an external service that is still up.
- The restore project and its volumes were removed after the drill.

**Recovery objectives this supports:**
- **RTO:** about 1 minute from empty volumes to verified, for this data size. The pieces that
  grow with data are `pg_restore` and the object import. Add the time to provision hosts. Record a
  drill on production-sized data before committing to a customer RTO.
- **RPO:** the backup interval, 24 h with the default `BACKUP_AT` schedule. Shorter needs PostgreSQL
  point-in-time recovery (WAL archiving), which this stack doesn't configure.

## 6. Failure drill: Redis outage on the production stack

1. **Redis stopped:**
   - sign-in attempts are still answered with normal 401s;
   - readiness 503, liveness 200;
   - the limiter logs `Rate limiter degraded` once per process.
2. **Defect D5-1:** `/metrics` answered **500** throughout the outage. The queue-depth `LLEN` wasn't
   guarded, so monitoring went blind exactly when it was needed. Fixed: queue depth is omitted while
   Redis is unreachable (`documind_dependency_up{component="redis"} 0` reports the outage), and
   every scrape returns 200.
3. **Redis back:** readiness 200.
4. **Defect D5-2:** processes that had no rate-limited traffic after the restart kept reporting
   `documind_rate_limiter_degraded 1`, so the alert would keep firing after the outage ended. Fixed:
   `RedisRateLimiter.probe()` re-checks Redis (at most every retry interval), and `/metrics` calls
   it. Re-run: three scrapes after recovery all read 0.
5. **Tests:** both fixes are covered by `test_metrics_keep_answering_and_report_recovery`
   (integration) and two probe unit tests.

## 7. Deployment verification

| Check | Result |
|---|---|
| `smoke/run.sh up` from the current source: migrations, API, worker, beat, Nginx | healthy |
| `smoke/run.sh test`: Playwright journey over HTTPS (sign-up, upload, cited answer, team, 2FA, mobile, Phase 8 pages) | 5/5 passed |
| `smoke/run.sh tls` | 22/22 checks |

## 8. Limitations

- **Images:** the API, worker and frontend images were not rebuilt from their Dockerfiles. The
  backup image used a drill-only Dockerfile with the same contents except where the PostgreSQL
  client came from. The `backup` stage fix (D5-4) is in `backend/Dockerfile`, but that stage
  hasn't been built here.
- **Monitoring:** no Prometheus or Alertmanager server ran. The metrics were read directly, and
  the alert rules were checked for syntax and runbook links only.
- **Offsite copy and real Let's Encrypt issuance:** not exercised.
- **Data size:** the drill data is small. The timings show the procedure works and how its steps
  are ordered, not how long a large database takes.
