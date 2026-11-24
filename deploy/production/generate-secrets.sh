#!/usr/bin/env sh
# Print fresh production secrets in .env format, and create the backup passphrase file.
#   ./generate-secrets.sh >> .env
# Store a copy of CREDENTIAL_ENCRYPTION_KEYS, MFA_ENCRYPTION_KEY and the backup
# passphrase in your secrets manager: without them, backups can't be fully restored.
set -eu
b64() { openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '\n='; }

echo "JWT_SECRET_KEY=$(b64 48)"
echo "MFA_ENCRYPTION_KEY=$(b64 32)"
echo "CREDENTIAL_ENCRYPTION_KEYS=k$(date -u +%Y%m%d):$(b64 32)"
echo "REDIS_PASSWORD=$(b64 24)"
echo "METRICS_TOKEN=$(b64 32)"

dir=$(dirname "$0")/secrets
mkdir -p "$dir"
chmod 700 "$dir"
if [ ! -s "$dir/backup_passphrase" ]; then
  umask 077
  b64 32 > "$dir/backup_passphrase"
  echo "# wrote $dir/backup_passphrase" >&2
fi
