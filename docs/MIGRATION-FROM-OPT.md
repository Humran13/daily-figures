# Migrate storage from `/opt`

Source: `/opt/daily-figures/data/production.db`

Target: `/home/<actual-site-user>/htdocs/<domain>/data/production.db`

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

The installer detects the legacy database automatically. When the website
destination has no database, it verifies the source, stops legacy writes,
copies through SQLite, copies uploads only into an empty target, fixes
ownership, migrates once, starts, and verifies integrity/mount/health. Original
legacy data remains intact. It never overwrites an existing website database.

If both paths contain data, the command stops. Never merge automatically;
inspect and deliberately use `daily-figures restore --from ...` for the chosen
dataset.

Before any successful schema migration, the unchanged legacy mount can be
started for rollback:

```bash
cd /opt/daily-figures
docker compose -f docker-compose.yml up -d daily-figures
```

After migration starts, do not assume old code/schema compatibility. Keep the
app stopped, inspect state, and restore a verified final-cutover snapshot through
the managed workflow if required.
