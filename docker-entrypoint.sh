#!/bin/sh
# Schema changes and backups are intentionally NOT performed here. Production
# migrations are controlled, locked operations in scripts/daily-figures. This
# keeps an ordinary container restart from changing production data.
set -e
export FLASK_APP=app.py

if [ -z "$SECRET_KEY" ]; then
    echo "FATAL: SECRET_KEY environment variable is required and was not set. Refusing to start." >&2
    exit 1
fi

exec gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
