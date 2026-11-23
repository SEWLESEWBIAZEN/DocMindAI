# Configuration reference

All backend configuration is read from environment variables by
`backend/src/documind/core/config.py` (pydantic-settings). Names are
case-insensitive. In native development the backend also reads `../.env`
(repository root) and then `backend/.env`, with the latter taking precedence.
Real environment variables always win over `.env` files.

Required variables have no default; the backend refuses to start without them.

## Backend application

| Variable                         | Required | Default        | Description                                                                 |
| -------------------------------- | -------- | -------------- | --------------------------------------------------------------------------- |
| `ENVIRONMENT`                    |          | `development`  | `development`, `test` or `production`.                                       |
| `APP_NAME`                       |          | `DocuMind AI`  | Display name in health output and OpenAPI.                                  |
| `LOG_LEVEL`                      |          | `INFO`         | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.                             |
| `LOG_FORMAT`                     |          | `json`         | `json` (one object per line) or `console`.                                  |
| `API_DOCS_ENABLED`               |          | *(unset)*      | Force OpenAPI docs on/off. Unset means on, except in production.             |
| `CORS_ALLOWED_ORIGINS`           |          | *(empty)*      | Comma-separated origins. Empty means no CORS middleware (same-origin only). `*` is rejected in production. |
| `HEALTH_CHECK_TIMEOUT_SECONDS`   |          | `3.0`          | Per-dependency timeout for readiness checks (max 30).                       |

## PostgreSQL

| Variable                          | Required | Default | Description                                                     |
| --------------------------------- | -------- | ------- | --------------------------------------------------------------- |
| `DATABASE_URL`                    | yes      |         | Must use `postgresql+asyncpg://user:pass@host:port/db`.          |
| `DATABASE_POOL_SIZE`              |          | `10`    | Persistent connections per process.                             |
| `DATABASE_MAX_OVERFLOW`           |          | `10`    | Extra burst connections per process.                            |
| `DATABASE_POOL_TIMEOUT_SECONDS`   |          | `10`    | Wait time for a pooled connection.                              |
| `DATABASE_ECHO`                   |          | `false` | Log all SQL (development debugging only).                       |

## Redis

| Variable                         | Required | Default | Description                                        |
| -------------------------------- | -------- | ------- | -------------------------------------------------- |
| `REDIS_URL`                      | yes      |         | `redis://[:password@]host:port/db` or `rediss://`. |
| `REDIS_SOCKET_TIMEOUT_SECONDS`   |          | `5.0`   | Connect and socket timeout.                        |

## Object storage (S3-compatible)

| Variable                        | Required | Default     | Description                                                       |
| ------------------------------- | -------- | ----------- | ----------------------------------------------------------------- |
| `S3_ENDPOINT_URL`               |          | *(AWS)*     | Set for MinIO or other S3-compatible services.                    |
| `S3_REGION`                     |          | `us-east-1` | Region; also used as the bucket location constraint.              |
| `S3_ACCESS_KEY_ID`              | yes      |             | Access key.                                                       |
| `S3_SECRET_ACCESS_KEY`          | yes      |             | Secret key.                                                       |
| `S3_BUCKET`                     | yes      |             | Private bucket for documents (lowercase DNS-style name, 3-63 chars). |
| `S3_FORCE_PATH_STYLE`           |          | `true`      | Path-style addressing (required by MinIO).                        |
| `S3_CONNECT_TIMEOUT_SECONDS`    |          | `5.0`       | boto3 connect timeout.                                            |
| `S3_READ_TIMEOUT_SECONDS`       |          | `30.0`      | boto3 read timeout.                                               |
| `S3_PUBLIC_ENDPOINT_URL`        |          | *(= `S3_ENDPOINT_URL`)* | Browser-reachable endpoint used **only** to sign download URLs (e.g. `http://localhost:9000` when the API reaches MinIO as `minio:9000`). |

## Documents

| Variable                  | Default    | Description                                                              |
| ------------------------- | ---------- | ------------------------------------------------------------------------ |
| `MAX_UPLOAD_BYTES`        | `52428800` | Largest accepted file (50 MiB). Request bodies above this + 1 MiB are cut off with 413. |
| `SIGNED_URL_TTL_SECONDS`  | `300`      | Lifetime of download URLs (30–3600).                                     |
| `MALWARE_SCANNER`         | `none`     | `none` or `clamav`. With `clamav`, uploads fail with 503 if clamd is unreachable. |
| `CLAMAV_HOST`             | `clamav`   | clamd host.                                                              |
| `CLAMAV_PORT`             | `3310`     | clamd TCP port.                                                          |
| `CLAMAV_TIMEOUT_SECONDS`  | `30`       | Scan timeout.                                                            |

## Authentication

| Variable                          | Required | Default            | Description                                                         |
| --------------------------------- | -------- | ------------------ | ------------------------------------------------------------------- |
| `JWT_SECRET_KEY`                  | yes      |                    | HS256 signing key for access tokens; at least 32 characters (use ≥ 48 random bytes). |
| `JWT_ISSUER`                      |          | `documind`         | `iss` claim.                                                        |
| `JWT_AUDIENCE`                    |          | `documind-api`     | `aud` claim.                                                        |
| `ACCESS_TOKEN_TTL_SECONDS`        |          | `900`              | Access token lifetime (60–3600).                                    |
| `REFRESH_TOKEN_TTL_DAYS`          |          | `14`               | Refresh token lifetime; slides on each rotation.                    |
| `REFRESH_COOKIE_NAME`             |          | `documind_refresh` | Name of the HttpOnly refresh cookie.                                |
| `AUTH_COOKIE_SECURE`              |          | `true`             | `Secure` cookie flag. Must be true in production; browsers accept it on `http://localhost`. |
| `EMAIL_VERIFICATION_TTL_HOURS`    |          | `24`               | Verification link lifetime.                                         |
| `PASSWORD_RESET_TTL_MINUTES`      |          | `30`               | Reset link lifetime.                                                |
| `INVITATION_TTL_DAYS`             |          | `7`                | Invitation lifetime.                                                |
| `PASSWORD_HASH_TIME_COST`         |          | `3`                | Argon2id iterations.                                                |
| `PASSWORD_HASH_MEMORY_KIB`        |          | `65536`            | Argon2id memory (64 MiB).                                           |
| `PASSWORD_HASH_PARALLELISM`       |          | `4`                | Argon2id lanes.                                                     |
| `PUBLIC_APP_URL`                  |          | `http://localhost:5173` | Web app base URL used in email links. Must be `https://` in production. |

## Two-factor authentication

| Variable                          | Required | Default    | Description                                                              |
| --------------------------------- | -------- | ---------- | ------------------------------------------------------------------------ |
| `MFA_ENCRYPTION_KEY`              | yes      |            | 32 random bytes, base64url. Encrypts TOTP secrets (AES-256-GCM) and keys recovery-code hashes. Generate with `python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`. Placeholders are rejected in production. |
| `MFA_ISSUER`                      |          | `DocuMind` | Name shown in authenticator apps.                                        |
| `MFA_STEP_UP_TTL_SECONDS`         |          | `300`      | How long a 2FA check authorizes sensitive actions such as deleting documents (30–3600). |
| `MFA_SETUP_TTL_MINUTES`           |          | `15`       | Time allowed to confirm a started setup.                                 |
| `RATE_LIMIT_MFA_PER_USER`         |          | `5`        | 2FA attempts (setup, confirm, verify, disable, regenerate) per user per window. |
| `RATE_LIMIT_MFA_WINDOW_SECONDS`   |          | `300`      | Window for the above.                                                    |

## Document processing

| Variable                              | Default  | Description                                                         |
| ------------------------------------- | -------- | ------------------------------------------------------------------- |
| `CELERY_BROKER_URL`                   | *(= `REDIS_URL`)* | Celery broker.                                             |
| `PROCESSING_MAX_ATTEMPTS`             | `5`      | Attempts before a job is marked `dead` (dead letter).               |
| `PROCESSING_RETRY_BASE_SECONDS`       | `30`     | Backoff base; doubles each attempt (±20 % jitter, capped at 1 h).   |
| `PROCESSING_LEASE_SECONDS`            | `1800`   | A running job whose lease expires is re-queued by the sweeper. Also bounds task time limits. |
| `PROCESSING_DISPATCH_GRACE_SECONDS`   | `60`     | Queued jobs not picked up within this time are re-dispatched.       |
| `PROCESSING_SWEEP_INTERVAL_SECONDS`   | `30`     | How often Celery beat runs the sweeper.                             |
| `MAX_DOCUMENT_PAGES`                  | `2000`   | PDFs with more pages fail permanently.                              |
| `CHUNK_MAX_TOKENS`                    | `500`    | Chunk budget (estimated tokens).                                    |
| `CHUNK_OVERLAP_TOKENS`                | `60`     | Overlap of whole sentences between consecutive chunks (< half the budget). |
| `CHUNK_RESPECT_PAGES`                 | `true`   | Never let a chunk span pages, so citations name exact pages.        |
| `OCR_ENABLED`                         | `true`   | OCR pages without a usable text layer (needs Tesseract language data). |
| `OCR_LANGUAGES`                       | `eng`    | Tesseract languages, e.g. `eng+deu` (install matching `tesseract-ocr-*` packages). |
| `OCR_DPI`                             | `300`    | Rendering resolution for OCR.                                       |

## Embeddings

| Variable                     | Default                   | Description                                                   |
| ---------------------------- | ------------------------- | ------------------------------------------------------------- |
| `EMBEDDING_PROVIDER`         | `openai`                  | `openai`, or `local` (deterministic, offline; development and tests only; rejected in production). |
| `OPENAI_API_KEY`             | *(none)*                  | Required when the provider is `openai`.                        |
| `OPENAI_BASE_URL`            | *(OpenAI)*                | Optional compatible endpoint (e.g. Azure OpenAI gateway).      |
| `EMBEDDING_MODEL`            | `text-embedding-3-small`  | Changing it: run `documind-manage reindex`.                    |
| `EMBEDDING_DIMENSIONS`       | `1536`                    | Must match the vector column (migration `0005`).               |
| `EMBEDDING_BATCH_SIZE`       | `64`                      | Inputs per request (also bounded by a token budget).           |
| `EMBEDDING_MAX_RETRIES`      | `5`                       | SDK retries for 429/5xx/connection errors (honours `Retry-After`). |
| `EMBEDDING_TIMEOUT_SECONDS`  | `60`                      | Per-request timeout.                                          |

## Answer generation (LLM)

| Variable                  | Default         | Description                                                   |
| ------------------------- | --------------- | ------------------------------------------------------------- |
| `LLM_PROVIDER`            | `openai`        | `openai`, or `local` (offline extractive stand-in; development and tests only; rejected in production). Uses `OPENAI_API_KEY` / `OPENAI_BASE_URL`. |
| `LLM_MODEL`               | `gpt-5.4-mini`  | Chat model for answers.                                       |
| `LLM_TEMPERATURE`         | *(unset)*       | Sent only when set (reasoning models reject custom values).   |
| `LLM_MAX_OUTPUT_TOKENS`   | `1200`          | Upper bound for an answer (`max_completion_tokens`).          |
| `LLM_TIMEOUT_SECONDS`     | `60`            | Per-request timeout.                                          |
| `LLM_MAX_RETRIES`         | `2`             | SDK retries for 429/5xx/connection errors.                    |

## Retrieval

| Variable                  | Default | Description                                                          |
| ------------------------- | ------- | -------------------------------------------------------------------- |
| `RETRIEVAL_TOP_K`         | `8`     | Passages returned by search and given to the model (requests may set 1–50). |
| `RETRIEVAL_CANDIDATES`    | `40`    | Candidates fetched from each retriever before fusion.                |
| `RETRIEVAL_MIN_SCORE`     | `0.2`   | Minimum cosine similarity for a vector hit (`0` disables it).        |
| `RETRIEVAL_HYBRID`        | `true`  | Combine vector and keyword (Postgres full-text) results with reciprocal-rank fusion. |
| `RETRIEVAL_EF_SEARCH`     | `100`   | HNSW `ef_search` (recall vs speed).                                  |
| `RAG_CONTEXT_MAX_TOKENS`  | `6000`  | Token budget for the sources sent to the model.                      |

## Rate limits

Fixed windows in Redis. Exceeding one returns 429 with `Retry-After`. If
Redis is unavailable, requests are allowed and an error is logged.

| Variable                              | Default | Applies to                                              |
| ------------------------------------- | ------- | ------------------------------------------------------- |
| `RATE_LIMIT_ENABLED`                  | `true`  | Master switch.                                          |
| `RATE_LIMIT_LOGIN_PER_IP`             | `20`    | Login attempts per client IP per window.                |
| `RATE_LIMIT_LOGIN_PER_EMAIL`          | `10`    | Login attempts per email per window; also password changes per user. |
| `RATE_LIMIT_LOGIN_WINDOW_SECONDS`     | `300`   | Login window.                                           |
| `RATE_LIMIT_AUTH_PER_IP`              | `10`    | Register, verify, resend, forgot and reset, per IP (and per email where given). |
| `RATE_LIMIT_AUTH_WINDOW_SECONDS`      | `600`   | Window for the above.                                   |
| `RATE_LIMIT_REFRESH_PER_IP`           | `60`    | Token refreshes per IP per window.                      |
| `RATE_LIMIT_REFRESH_WINDOW_SECONDS`   | `60`    | Refresh window.                                         |
| `RATE_LIMIT_AI_PER_USER`              | `30`    | Search and answer requests per user per window.         |
| `RATE_LIMIT_AI_WINDOW_SECONDS`        | `60`    | Window for the above.                                   |

## Email (SMTP)

| Variable                | Default                                  | Description                                     |
| ----------------------- | ---------------------------------------- | ----------------------------------------------- |
| `SMTP_HOST`             | `localhost`                              | SMTP server (`mailpit` inside Compose).          |
| `SMTP_PORT`             | `1025`                                   |                                                 |
| `SMTP_USERNAME`         | *(none)*                                 | Login is used only when username and password are both set. |
| `SMTP_PASSWORD`         | *(none)*                                 |                                                 |
| `SMTP_STARTTLS`         | `false`                                  | Upgrade with STARTTLS. Production requires this or SSL. |
| `SMTP_USE_SSL`          | `false`                                  | Implicit TLS (usually port 465). Mutually exclusive with STARTTLS. |
| `SMTP_TIMEOUT_SECONDS`  | `10`                                     |                                                 |
| `EMAIL_FROM`            | `DocuMind AI <no-reply@documind.local>`  | From header.                                    |

## Production safeguards

With `ENVIRONMENT=production` the backend refuses to start if:

- `DATABASE_URL`, `REDIS_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` or
  `JWT_SECRET_KEY` (or the decoded `MFA_ENCRYPTION_KEY`) contains a development placeholder marker (`change-me`,
  `dev-only`, `insecure`);
- `CORS_ALLOWED_ORIGINS` contains `*`;
- `AUTH_COOKIE_SECURE` is false;
- `PUBLIC_APP_URL` is not `https://`;
- SMTP uses neither STARTTLS nor SSL;
- `EMBEDDING_PROVIDER` or `LLM_PROVIDER` is not `openai`.

## Docker Compose (development) variables

Read by `docker-compose.yml` from the repository-root `.env`:

| Variable                   | Default | Description                                             |
| -------------------------- | ------- | ------------------------------------------------------- |
| `POSTGRES_USER`            | n/a     | Database superuser and application user (development). |
| `POSTGRES_PASSWORD`        | n/a     | Its password.                                           |
| `POSTGRES_DB`              | n/a     | Application database; `<db>_test` is created alongside. |
| `REDIS_PASSWORD`           | n/a     | Redis `requirepass`.                                    |
| `MINIO_ROOT_USER`          | n/a     | MinIO root user (also the backend's S3 key in development). |
| `MINIO_ROOT_PASSWORD`      | n/a     | MinIO root password.                                    |
| `S3_BUCKET`                | n/a     | Bucket name passed to the backend container.            |
| `MINIO_IMAGE`              | `bitnamilegacy/minio:2025.7.23` | See ADR 0006.                   |
| `POSTGRES_HOST_PORT`       | `5432`  | Host port (bound to 127.0.0.1).                         |
| `REDIS_HOST_PORT`          | `6379`  |                                                         |
| `MINIO_API_HOST_PORT`      | `9000`  |                                                         |
| `MINIO_CONSOLE_HOST_PORT`  | `9001`  |                                                         |
| `BACKEND_HOST_PORT`        | `8000`  |                                                         |
| `FRONTEND_HOST_PORT`       | `5173`  |                                                         |
| `MAILPIT_SMTP_HOST_PORT`   | `1025`  | Mailpit SMTP (development mail catcher).                |
| `MAILPIT_UI_HOST_PORT`     | `8025`  | Mailpit web UI: read every email the app sends.         |

Inside Compose, the backend container receives `DATABASE_URL`, `REDIS_URL` and
the `S3_*` values built from the infrastructure variables above, using service
hostnames (`postgres`, `redis`, `minio`). The `localhost` URLs in `.env` apply
only to native runs. Compose defaults `EMBEDDING_PROVIDER` and `LLM_PROVIDER`
to `local`, so the stack runs without an API key.

## Frontend

| Variable                 | Default                 | Description                                   |
| ------------------------ | ----------------------- | --------------------------------------------- |
| `VITE_API_PROXY_TARGET`  | `http://localhost:8000` | Where the Vite dev server proxies `/api`.     |

## Runtime image (uvicorn)

| Variable              | Description                                                                          |
| --------------------- | ------------------------------------------------------------------------------------ |
| `FORWARDED_ALLOW_IPS` | IPs trusted to set `X-Forwarded-*` headers (set to the reverse proxy in Phase 10).   |
