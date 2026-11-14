#!/usr/bin/env bash
# Restore a backup.sh dump into the compose stack's database
# (docs/operations.md §6.2; rollback across migrations: §5.2).
#
#   ./restore.sh backups/akkiabot-<UTC timestamp>.dump
#
# Run from the directory holding compose.yaml and .env. The script:
#   1. stops app and cloudflared (no writes during the restore);
#   2. drops and recreates the database, so tables or columns added by a newer
#      migration cannot survive the restore;
#   3. restores the dump (stops at the first error) and prints the restored
#      Alembic revision.
# It never starts app or migrate: start the stack yourself, with the image
# matching the dump, once the printed revision is the expected one.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <dump file>" >&2
    exit 2
fi
dump="$1"
if ! [[ -f "$dump" && -s "$dump" ]]; then
    echo "restore: dump not found or empty: $dump" >&2
    exit 2
fi

compose=(docker compose)

"${compose[@]}" stop app cloudflared
"${compose[@]}" up -d --wait postgres

echo "restore: recreating database akkiabot"
"${compose[@]}" exec -T postgres dropdb -U akkiabot --if-exists --force akkiabot
"${compose[@]}" exec -T postgres createdb -U akkiabot -O akkiabot akkiabot

echo "restore: restoring $dump"
"${compose[@]}" exec -T postgres pg_restore -U akkiabot -d akkiabot --exit-on-error <"$dump"

revision="$("${compose[@]}" exec -T postgres psql -U akkiabot -d akkiabot -tAc 'select version_num from alembic_version')"
echo "restore: done, alembic revision: ${revision:-<none>}"
echo "restore: app and cloudflared are stopped; start them with the image matching this revision (docker compose up -d --wait)"
