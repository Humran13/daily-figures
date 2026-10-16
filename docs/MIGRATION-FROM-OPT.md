# Migrate storage from `/opt`

Source: `/opt/daily-figures/data/production.db`

Target: `/home/<actual-site-user>/app-data/daily-figures/production.db`

## Preflight and cutover

Confirm health/free space and create the correct CloudPanel Reverse Proxy site.
Inspect the old checkout before updating it:

```bash
git -C /opt/daily-figures status --short
curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh
```

The bootstrap recognizes an older Git checkout even when it has no
`scripts/daily-figures`. It fetches the expected `origin/main` and fast-forwards
only after verifying that tracked files have no staged or unstaged changes,
that `main` has not diverged, and that production data is not tracked by Git.
If a safety check fails, resolve it manually; do not force-reset the production
checkout. Existing `.env` files, SQLite databases, uploads, and backups remain
in place.

The installer does not migrate or copy a legacy database automatically. If it
finds `/opt/daily-figures/data/production.db` while private storage is
configured, it stops and reports the mismatch. Any cutover must be separately
reviewed and explicitly invoked with `daily-figures migrate-storage`; it never
overwrites an existing private database.

If both paths contain data, the command stops. Never merge automatically;
inspect and deliberately use `daily-figures restore --from ...` for the chosen
dataset.

Do not use a bare Compose command for a production start or update. After an
explicit cutover, use the management utility so it loads the saved production
paths and Compose overlay. After migration starts, do not assume old
code/schema compatibility; keep the app stopped, inspect state, and restore a
verified final-cutover snapshot through the managed workflow if required.
