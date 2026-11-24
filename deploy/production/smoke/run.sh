#!/usr/bin/env bash
# Production smoke test: start the production stack in production mode on this host
# (HTTPS on 127.0.0.1:8443 as localhost) and run the end-to-end journey against it.
#   deploy/production/smoke/run.sh [up|test|down]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work="$here/work"
compose=(docker compose -p documind-smoke --env-file "$work/smoke.env"
  -f "$here/../docker-compose.yml" -f "$here/docker-compose.smoke.yml"
  ${SMOKE_USE_SOURCE:+-f "$here/docker-compose.source.yml"} --profile bundled-db)

issue() {  # name dir cert key
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout "$2/$4" -out "$work/$1.csr" 2>/dev/null
  openssl x509 -req -in "$work/$1.csr" -CA "$work/ca.pem" -CAkey "$work/ca-key.pem" -CAcreateserial \
    -days 2 -out "$2/$3" -extfile <(printf "subjectAltName=DNS:%s" "$1") 2>/dev/null
  chmod 644 "$2/$3" "$2/$4"
}

up() {
  mkdir -p "$work/certs/live/localhost" "$work/mailpit" "$here/../secrets"
  if [ ! -s "$work/ca.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=DocuMind smoke CA" \
      -keyout "$work/ca-key.pem" -out "$work/ca.pem" 2>/dev/null
    issue localhost "$work/certs/live/localhost" fullchain.pem privkey.pem
    issue mailpit "$work/mailpit" mailpit.pem mailpit-key.pem
  fi
  [ -s "$here/../secrets/backup_passphrase" ] || openssl rand -hex 24 > "$here/../secrets/backup_passphrase"
  if [ ! -s "$work/smoke.env" ]; then
    {
      cat <<ENV
DOCUMIND_ENV_FILE=smoke/work/smoke.env
SERVER_NAME=localhost
LETSENCRYPT_EMAIL=ops@example.com
POSTGRES_PASSWORD=smoke-db-password-42
DATABASE_URL=postgresql+asyncpg://documind:smoke-db-password-42@postgres:5432/documind
S3_ENDPOINT_URL=http://minio:9000
S3_REGION=us-east-1
S3_BUCKET=documind-smoke
S3_ACCESS_KEY_ID=smoke-minio-user
S3_SECRET_ACCESS_KEY=smoke-minio-password-123
S3_FORCE_PATH_STYLE=true
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_STARTTLS=true
EMAIL_FROM=DocuMind <no-reply@example.com>
EMBEDDING_PROVIDER=openai
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-smoke-not-a-real-key-000000
OPENAI_BASE_URL=http://openai-stub:8080/v1
SPEECH_PROVIDER=none
RATE_LIMIT_AUTH_PER_IP=1000
RATE_LIMIT_LOGIN_PER_IP=1000
API_WORKERS=2
ENV
      "$here/../generate-secrets.sh" 2>/dev/null
    } > "$work/smoke.env"
  fi
  "${compose[@]}" up -d --wait --wait-timeout 300 web
  "${compose[@]}" up -d worker beat
  curl -fsS --noproxy "*" --resolve localhost:8443:127.0.0.1 --cacert "$work/ca.pem" \
    https://localhost:8443/api/v1/health >/dev/null
  echo "Production stack is up: https://localhost:8443 (CA: $work/ca.pem)"
}

test_journey() {
  cd "$here/../../../frontend"
  E2E_BASE_URL=https://localhost:8443 E2E_MAILPIT_URL=http://127.0.0.1:18025 \
    E2E_IGNORE_HTTPS_ERRORS=1 \
    npx playwright test e2e/journey.spec.ts e2e/saas.spec.ts --trace off --reporter=line
}

case "${1:-all}" in
  up) up ;;
  test) test_journey ;;
  down) "${compose[@]}" down -v ;;
  all) up && test_journey ;;
esac
