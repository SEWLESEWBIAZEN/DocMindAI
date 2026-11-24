#!/usr/bin/env bash
# Restore a DocuMind backup archive into EMPTY targets (never over live data).
#
# Usage: restore.sh <archive>
# Required:
#   PGURL                    libpq URL of the (empty) control database to restore into
#   BACKUP_PASSPHRASE_FILE   the archive passphrase
#   S3_BUCKET (and S3_*)     the (empty) bucket objects are restored into, via documind-manage
# Optional:
#   DATAPLANE_PGURLS         target URLs for the archive's data planes, in the same order
#   DOCUMIND_MANAGE          command for documind-manage (default: documind-manage)
#
# Afterwards run `alembic upgrade head` and `documind-manage check`. Customer credentials
# decrypt only with the original CREDENTIAL_ENCRYPTION_KEYS.
set -euo pipefail

archive=${1:?usage: restore.sh <archive>}
: "${PGURL:?set PGURL}"
: "${BACKUP_PASSPHRASE_FILE:?set BACKUP_PASSPHRASE_FILE}"
MANAGE=${DOCUMIND_MANAGE:-documind-manage}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077

echo "Decrypting and verifying..."
$MANAGE backup unseal "$archive" "$work/backup.tar" --passphrase-file "$BACKUP_PASSPHRASE_FILE"
mkdir "$work/x"
tar -C "$work/x" -xf "$work/backup.tar"
( cd "$work/x" && sha256sum --check --quiet SHA256SUMS )

tables=$(psql "$PGURL" -Atc "select count(*) from pg_tables where schemaname = 'public'")
if [ "$tables" != "0" ]; then
  echo "Refusing to restore: the target control database is not empty." >&2
  exit 1
fi
echo "Restoring control database..."
pg_restore --no-owner --no-privileges --exit-on-error --dbname "$PGURL" "$work/x/control.dump"

i=0
for url in ${DATAPLANE_PGURLS:-}; do
  i=$((i + 1))
  echo "Restoring data plane $i..."
  pg_restore --no-owner --no-privileges --exit-on-error --dbname "$url" "$work/x/dataplane-$i.dump"
done

echo "Restoring object storage..."
$MANAGE objects import "$work/x/objects" >/dev/null
echo "Restore complete. Run: alembic upgrade head && documind-manage check"
