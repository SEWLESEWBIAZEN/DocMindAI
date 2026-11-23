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

## Production safeguards

With `ENVIRONMENT=production` the backend refuses to start if:

- `DATABASE_URL`, `REDIS_URL`, `S3_ACCESS_KEY_ID` or `S3_SECRET_ACCESS_KEY`
  contains a development placeholder marker (`change-me`, `dev-only`, `insecure`);
- `CORS_ALLOWED_ORIGINS` contains `*`.

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

Inside Compose, the backend container receives `DATABASE_URL`, `REDIS_URL` and
the `S3_*` values built from the infrastructure variables above, using service
hostnames (`postgres`, `redis`, `minio`). The `localhost` URLs in `.env` apply
only to native runs.

## Frontend

| Variable                 | Default                 | Description                                   |
| ------------------------ | ----------------------- | --------------------------------------------- |
| `VITE_API_PROXY_TARGET`  | `http://localhost:8000` | Where the Vite dev server proxies `/api`.     |

## Runtime image (uvicorn)

| Variable              | Description                                                                          |
| --------------------- | ------------------------------------------------------------------------------------ |
| `FORWARDED_ALLOW_IPS` | IPs trusted to set `X-Forwarded-*` headers (set to the reverse proxy in Phase 10).   |
