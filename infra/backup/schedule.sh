#!/usr/bin/env bash
# Run backup.sh every day at BACKUP_AT (UTC, HH:MM), then copy the archive offsite.
#   BACKUP_AT              default 02:30
#   BACKUP_OFFSITE_BUCKET  optional: separate bucket (ideally another account/region)
#   BACKUP_RUN_NOW=true    also run once at start (first deployment, drills)
set -euo pipefail
: "${BACKUP_DIR:=/backups}"
export BACKUP_DIR
at=${BACKUP_AT:-02:30}

run() {
  echo "$(date -u +%FT%TZ) backup starting"
  if backup.sh; then
    latest=$(ls -1t "$BACKUP_DIR"/documind-*.tar.dmbk | head -1)
    if [ -n "${BACKUP_OFFSITE_BUCKET:-}" ]; then
      documind-manage objects upload-file "$latest" --bucket "$BACKUP_OFFSITE_BUCKET"
    fi
    date -u +%s > "$BACKUP_DIR/.last_success"
    documind-manage backup mark-success || echo "could not record the backup in Redis (metrics)" >&2
    echo "$(date -u +%FT%TZ) backup finished: $latest"
  else
    echo "$(date -u +%FT%TZ) backup FAILED" >&2
  fi
}

[ "${BACKUP_RUN_NOW:-false}" = "true" ] && run
while :; do
  now=$(date -u +%s)
  next=$(date -u -d "today $at" +%s)
  [ "$next" -le "$now" ] && next=$(date -u -d "tomorrow $at" +%s)
  sleep $((next - now))
  run
done
