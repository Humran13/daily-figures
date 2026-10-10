#!/usr/bin/env bash
# One-line bootstrap. It obtains or safely fast-forwards the repository before
# handing control to the versioned management utility.
set -Eeuo pipefail

REPOSITORY_URL=${DAILY_FIGURES_REPOSITORY_URL:-https://github.com/Humran13/daily-figures.git}
CODE_DIR=${DAILY_FIGURES_CODE_DIR:-/opt/daily-figures}

fatal() {
    printf 'FATAL: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '[daily-figures bootstrap] %s\n' "$*"
}

canonical_repository() {
    local repository=${1%/}
    repository=${repository%.git}
    repository=${repository#https://github.com/}
    repository=${repository#ssh://git@github.com/}
    repository=${repository#git@github.com:}
    printf '%s\n' "$repository"
}

assert_no_tracked_production_data() {
    local path
    local protected=()

    while IFS= read -r -d '' path; do
        case "$path" in
            .env|.env.*)
                [[ $path == .env.example ]] || protected+=("$path")
                ;;
            data/*.db|data/*.db-*|data/uploads/*|data/backups/*|backups/*)
                protected+=("$path")
                ;;
        esac
    done < <(git -C "$CODE_DIR" ls-files -z)

    if ((${#protected[@]})); then
        printf 'FATAL: protected production data is tracked by Git; refusing to update:\n' >&2
        printf '  %s\n' "${protected[@]}" >&2
        printf 'Untrack and preserve these files manually before retrying. Nothing was changed.\n' >&2
        exit 1
    fi
}

update_existing_checkout() {
    local branch origin_url current_commit fetched_commit index_tag index_path

    log "Existing Git checkout detected at $CODE_DIR."
    if [[ ! -e $CODE_DIR/scripts/daily-figures ]]; then
        log 'Management utility is missing; a safe fast-forward update is required.'
    fi

    while read -r index_tag index_path; do
        case "$index_tag" in
            S|[a-z])
                fatal "tracked path '$index_path' has skip-worktree/assume-unchanged set; clear the index flag and verify it before retrying."
                ;;
        esac
    done < <(git -C "$CODE_DIR" ls-files -v)

    git -C "$CODE_DIR" diff --quiet -- || \
        fatal 'tracked working-tree modifications exist; commit or stash them before retrying.'
    git -C "$CODE_DIR" diff --cached --quiet -- || \
        fatal 'staged modifications exist; commit or stash them before retrying.'
    assert_no_tracked_production_data

    branch=$(git -C "$CODE_DIR" symbolic-ref --quiet --short HEAD) || \
        fatal 'the existing checkout has a detached HEAD; check out main before retrying.'
    [[ $branch == main ]] || fatal "the existing checkout is on '$branch', not 'main'."

    origin_url=$(git -C "$CODE_DIR" remote get-url origin 2>/dev/null) || \
        fatal 'the existing checkout has no origin remote.'
    if [[ $(canonical_repository "$origin_url") != $(canonical_repository "$REPOSITORY_URL") ]]; then
        fatal "origin points to '$origin_url', not '$REPOSITORY_URL'; refusing to fetch from an unexpected repository."
    fi

    log 'Fetching origin/main without touching the running application.'
    git -C "$CODE_DIR" fetch --no-tags origin main
    current_commit=$(git -C "$CODE_DIR" rev-parse HEAD)
    fetched_commit=$(git -C "$CODE_DIR" rev-parse FETCH_HEAD)

    if [[ $current_commit != "$fetched_commit" ]]; then
        git -C "$CODE_DIR" merge-base --is-ancestor "$current_commit" "$fetched_commit" || \
            fatal 'local main is ahead of or has diverged from origin/main; refusing a non-fast-forward update.'
        git -C "$CODE_DIR" merge --ff-only "$fetched_commit"
        log "Fast-forwarded main to $fetched_commit."
    else
        log 'The checkout is already at the requested main revision.'
    fi
}

if [[ ${DAILY_FIGURES_BOOTSTRAP_TEST_MODE:-0} != 1 && ${EUID:-$(id -u)} -ne 0 ]]; then
    printf 'FATAL: run this installer as root (use sudo).\n' >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates
fi

if [[ -e $CODE_DIR && ! -d $CODE_DIR/.git ]]; then
    fatal "$CODE_DIR exists but is not a Git checkout; refusing to overwrite it."
fi
if [[ ! -d $CODE_DIR/.git ]]; then
    install -d -m 755 "${CODE_DIR%/*}"
    git clone --branch main --single-branch "$REPOSITORY_URL" "$CODE_DIR"
else
    update_existing_checkout
fi

[[ -f $CODE_DIR/scripts/daily-figures ]] || \
    fatal 'scripts/daily-figures is still missing after the repository update.'
chmod 755 "$CODE_DIR/scripts/daily-figures"
exec "$CODE_DIR/scripts/daily-figures" install "$@"
