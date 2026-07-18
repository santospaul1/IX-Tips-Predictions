#!/bin/sh
# Daily Postgres backup — run by supercronic on the cron machine.
# Stores the last 7 daily dumps in /code/backups/ (ephemeral).
# For long-term retention, add an S3/Backblaze upload step.
set -e

BACKUP_DIR="/code/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d)
DUMP_FILE="$BACKUP_DIR/ix_tips_$TIMESTAMP.dump"

# pg_dump with the DATABASE_URL from Fly Postgres
echo "[backup] Dumping to $DUMP_FILE"
pg_dump -Fc --no-owner --no-acl "$DATABASE_URL" -f "$DUMP_FILE"

# Remove dumps older than 7 days
find "$BACKUP_DIR" -name "*.dump" -mtime +7 -delete

echo "[backup] Done — $(ls -lh "$DUMP_FILE" | awk '{print $5}')"
