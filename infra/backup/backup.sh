#!/usr/bin/env bash
# DocuMind backup: control database, DocuMind-managed data planes and object storage,
# in one encrypted, checksummed archive (Phase 9; scheduling and offsite copy: Phase 10).
#
# Required:
#   PGURL                    libpq URL of the control database (postgresql://user:pass@host/db)
#   BACKUP_DIR               where archives are written
#   BACKUP_PASSPHRASE_FILE   file holding the archive passphrase (>= 16 characters)
# Optional:
#   DATAPLANE_PGURLS         space-separated libpq URLs of dedicated data-plane databases
#                            (external databases are backed up by their owners)
#   DOCUMIND_MANAGE          command for documind-manage (default: documind-manage)
#   RETENTION_DAYS           delete archives older than this (default: 14)
#
# The archive never holds plaintext customer credentials: they are stored encrypted
# (ADR 0022) and CREDENTIAL_ENCRYPTION_KEYS is NOT part of the backup. Back the key
# ring up separately (see docs/runbooks/backup-and-restore.md).
set -euo pipefail

: "${PGURL:?set PGURL}"
: "${BACKUP_DIR:?set BACKUP_DIR}"
: "${BACKUP_PASSPHRASE_FILE:?set BACKUP_PASSPHRASE_FILE (backups are always encrypted)}"
MANAGE=${DOCUMIND_MANAGE:-documind-manage}
RETENTION_DAYS=${RETENTION_DAYS:-14}

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077
mkdir -p "$BACKUP_DIR"

echo "Dumping control database..."
pg_dump --format=custom --no-owner --no-privileges --file "$work/control.dump" "$PGURL"

i=0
for url in ${DATAPLANE_PGURLS:-}; do
  i=$((i + 1))
  echo "Dumping data plane $i..."
  pg_dump --format=custom --no-owner --no-privileges --schema=documind \
    --file "$work/dataplane-$i.dump" "$url"
done

echo "Exporting object storage..."
$MANAGE objects export "$work/objects" >/dev/null

( cd "$work" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
printf '{"created_at": "%s", "dataplanes": %d}\n' "$stamp" "$i" > "$work/BACKUP_INFO.json"

archive="$BACKUP_DIR/documind-$stamp.tar.dmbk"
tar -C "$work" -cf "$work.tar" .
$MANAGE backup seal "$work.tar" "$archive" --passphrase-file "$BACKUP_PASSPHRASE_FILE"
rm -f "$work.tar"
echo "Archive: $archive ($(du -h "$archive" | cut -f1))"

find "$BACKUP_DIR" -name 'documind-*.tar.dmbk' -mtime +"$RETENTION_DAYS" -print -delete
