#!/bin/sh
# Usage: backup_db.sh <path-to-sqlite-database>
#
# Copies the database to <same-dir>/backups/<name>_<timestamp>.db before
# any migration runs. Exits non-zero (and leaves no partial file behind)
# if the backup cannot be created for any reason — the caller (see
# docker-entrypoint.sh) is expected to treat that as fatal and refuse to
# run `flask db upgrade` against an unbacked-up database.
#
# Never overwrites an existing backup: if a file with the same timestamp
# already exists (e.g. two restarts within the same second), a numeric
# suffix is appended instead.
set -e

DB_FILE="$1"
if [ -z "$DB_FILE" ]; then
    echo "Usage: $0 <path-to-database>" >&2
    exit 2
fi

if [ ! -f "$DB_FILE" ]; then
    echo "No database found at $DB_FILE — nothing to back up (first-ever startup)." >&2
    exit 0
fi

BACKUP_DIR="$(dirname "$DB_FILE")/backups"
if ! mkdir -p "$BACKUP_DIR"; then
    echo "FATAL: could not create backup directory $BACKUP_DIR" >&2
    exit 1
fi

BASE_NAME="$(basename "$DB_FILE" .db)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${BASE_NAME}_${TIMESTAMP}.db"

SUFFIX=0
while [ -f "$BACKUP_FILE" ]; do
    SUFFIX=$((SUFFIX + 1))
    BACKUP_FILE="$BACKUP_DIR/${BASE_NAME}_${TIMESTAMP}_${SUFFIX}.db"
    if [ "$SUFFIX" -gt 1000 ]; then
        echo "FATAL: could not find a free backup filename after 1000 attempts" >&2
        exit 1
    fi
done

if ! cp "$DB_FILE" "$BACKUP_FILE"; then
    echo "FATAL: backup copy to $BACKUP_FILE failed — refusing to proceed without a successful backup." >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

echo "Backed up $DB_FILE -> $BACKUP_FILE"
