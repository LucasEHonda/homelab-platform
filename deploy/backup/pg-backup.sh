#!/bin/sh
# Nightly logical backup of every app database, next to the ZFS snapshots of the datasets.
# Writes /backups/<db>/<db>-YYYY-MM-DD_HHMM.dump (pg_dump custom format, restorable with
# pg_restore) and deletes dumps older than BACKUP_KEEP_DAYS in that folder. Run as the
# data-backup service of deploy/compose.yml.
#   pg-backup.sh          schedule: dump all of BACKUP_DATABASES daily at BACKUP_AT
#   pg-backup.sh now      dump all of BACKUP_DATABASES once and exit
#   pg-backup.sh now <db> dump only <db> once and exit (used by the deployer before a deploy)
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${BACKUP_DATABASES:?}"
export PGPASSWORD="$POSTGRES_PASSWORD"
BACKUP_AT="${BACKUP_AT:-03:30}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

backup_one() {
  db=$1
  stamp=$(date +%Y-%m-%d_%H%M)
  target="/backups/$db/$db-$stamp.dump"
  # Dump to a partial file so a failed run never leaves a truncated .dump to be restored.
  if ! pg_dump --format=custom --no-owner --dbname "$db" --username "$POSTGRES_USER" \
    --file "$target.partial"; then
    rm -f "$target.partial"
    echo "backup: FAILED $db" >&2
    return 1
  fi
  mv "$target.partial" "$target"
  find "/backups/$db" -name "$db-*.dump" -mtime +"$BACKUP_KEEP_DAYS" -delete
  echo "backup: wrote $target ($(du -h --apparent-size "$target" | cut -f1))"
}

backup_all() {
  failed=0
  for db in "$@"; do
    backup_one "$db" || failed=1
  done
  return "$failed"
}

if [ "${1:-}" = "now" ]; then
  if [ -n "${2:-}" ]; then
    backup_all "$2"
  else
    # shellcheck disable=SC2086
    backup_all $BACKUP_DATABASES
  fi
  exit $?
fi

while true; do
  now=$(date +%s)
  next=$(date -d "$(date +%Y-%m-%d) $BACKUP_AT" +%s)
  if [ "$next" -le "$now" ]; then
    next=$(date -d "tomorrow $BACKUP_AT" +%s)
  fi
  echo "backup: next run at $(date -d "@$next" '+%Y-%m-%d %H:%M')"
  sleep $((next - now))
  # shellcheck disable=SC2086
  backup_all $BACKUP_DATABASES || echo "backup: some dumps FAILED" >&2
done
