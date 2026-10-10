#!/bin/sh
# Compatibility wrapper. Uses SQLite's online backup API; never copies a live
# database file byte-for-byte. Production normally passes an explicit external
# backup directory as argument two.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 DATABASE [BACKUP_DIRECTORY]" >&2
    exit 2
fi

DB_FILE=$1
BACKUP_DIR=${2:-"$(dirname "$DB_FILE")/backups"}
SCRIPT_DIR=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)

if [ ! -f "$DB_FILE" ]; then
    echo "No database found at $DB_FILE; nothing to back up." >&2
    exit 0
fi

exec python3 "$SCRIPT_DIR/sqlite_backup.py" create \
    --source "$DB_FILE" --destination "$BACKUP_DIR"
