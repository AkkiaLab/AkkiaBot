#!/usr/bin/env bash
# PostgreSQL backup for the compose stack (docs/operations.md §备份与恢复).
#
# Run from the directory holding compose.yaml and .env, e.g. from cron:
#   17 3 * * * cd /opt/akkiabot/production && ./backup.sh >> backups/backup.log 2>&1
#
# Writes backups/akkiabot-<UTC timestamp>.dump (pg_dump custom format) and
# keeps the newest $BACKUP_KEEP files (default 14). The dump is written to a
# temporary file first, so a failed run never leaves a truncated backup.
set -euo pipefail

backup_dir="${BACKUP_DIR:-backups}"
keep="${BACKUP_KEEP:-14}"
compose=(docker compose)

if ! [[ "$keep" =~ ^[1-9][0-9]*$ ]]; then
    echo "backup: BACKUP_KEEP must be a positive integer" >&2
    exit 2
fi

umask 077
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/akkiabot-$stamp.dump"
tmp="$target.partial"
trap 'rm -f "$tmp"' EXIT

"${compose[@]}" exec -T postgres pg_dump -U akkiabot -d akkiabot --format=custom >"$tmp"
if ! [[ -s "$tmp" ]]; then
    echo "backup: pg_dump produced no output" >&2
    exit 1
fi
mv "$tmp" "$target"
echo "backup: wrote $target ($(wc -c <"$target") bytes)"

# Rotate: newest first, drop everything past $keep.
mapfile -t old < <(find "$backup_dir" -maxdepth 1 -name 'akkiabot-*.dump' -type f | sort -r | tail -n +"$((keep + 1))")
for f in "${old[@]}"; do
    rm -f -- "$f"
    echo "backup: removed $f"
done
