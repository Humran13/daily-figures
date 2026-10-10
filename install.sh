#!/usr/bin/env bash
# One-line bootstrap. It only obtains the repository; all safety logic lives in
# the versioned management utility.
set -Eeuo pipefail

REPOSITORY_URL=${DAILY_FIGURES_REPOSITORY_URL:-https://github.com/Humran13/daily-figures.git}
CODE_DIR=${DAILY_FIGURES_CODE_DIR:-/opt/daily-figures}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    printf 'FATAL: run this installer as root (use sudo).\n' >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates
fi

if [[ -e $CODE_DIR && ! -d $CODE_DIR/.git ]]; then
    printf 'FATAL: %s exists but is not a Git checkout; refusing to overwrite it.\n' "$CODE_DIR" >&2
    exit 1
fi
if [[ ! -d $CODE_DIR/.git ]]; then
    install -d -m 755 "${CODE_DIR%/*}"
    git clone --branch main --single-branch "$REPOSITORY_URL" "$CODE_DIR"
fi

chmod 755 "$CODE_DIR/scripts/daily-figures"
exec "$CODE_DIR/scripts/daily-figures" install "$@"
