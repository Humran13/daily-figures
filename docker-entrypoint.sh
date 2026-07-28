#!/bin/sh
# Backs up the database, then brings the schema up to date, before the app
# starts accepting traffic. Migrations are additive-only (see
# migrations/versions/), but the backup runs regardless — cheap insurance
# against the unexpected. If the backup fails for any reason, this script
# aborts (set -e + backup_db.sh's own non-zero exit on failure) and
# `flask db upgrade` never runs against an unbacked-up database.
set -e
export FLASK_APP=app.py

if [ -z "$SECRET_KEY" ]; then
    echo "FATAL: SECRET_KEY environment variable is required and was not set. Refusing to start." >&2
    exit 1
fi

DB_FILE="${DB_PATH:-/app/data/production.db}"
"$(dirname "$0")/scripts/backup_db.sh" "$DB_FILE"

flask db upgrade
exec gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
