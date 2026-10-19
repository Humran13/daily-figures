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
reviewed and explicitly invoked with
`daily-figures migrate-storage --source /absolute/path/to/production.db`; it
never overwrites an existing private database unless
`--preserve-existing-destination` is given, and even then the existing
destination is archived under a timestamped name, never deleted.

If both paths contain data, the command stops. Never merge automatically;
inspect and deliberately choose the dataset with an explicit `--source`, or
use `daily-figures restore --from ...` instead.

Do not use a bare Compose command for a production start or update. After an
explicit cutover, use the management utility so it loads the saved production
paths and Compose overlay. After migration starts, do not assume old
code/schema compatibility; keep the app stopped, inspect state, and restore a
verified final-cutover snapshot through the managed workflow if required.

## Recovering from a legacy/unsafe `install.conf`

Before commit `443fc66`, `install.conf` could still contain
`DATA_DIR`/`CONFIG_DIR`/`BACKUP_ROOT` pointing beneath
`htdocs/<domain>/data`. Every command that loads state (including, before
this fix, `migrate-storage --help`) correctly refuses to run against such a
file -- but that left no supported way to fix the file itself, since the
very commands needed to repair it also refused to start.

`daily-figures repair-installation [--yes]` is the dedicated, tested fix. It
does **not** require `install.conf` to pass normal validation first. It
trusts only identity fields from the existing file (domain, site user/home,
code directory, port, Compose file name, backup schedule) -- never the
legacy storage paths as destinations. It re-discovers the CloudPanel site
the same way `install` does, recomputes the required private
`app-data`/`app-config`/`app-backups` paths under the site's real home
directory, backs up `install.conf` with a timestamp (never deleted) before
writing the repaired state atomically, and creates the private directories.

`.env` recovery follows strict, non-destructive rules:

- Target present, no legacy copy found -> kept as-is.
- Legacy copy found, target missing -> copied to the target, mode `0600`.
- Both present and byte-identical -> kept as-is.
- Both present and different -> **stop**; this is never decided
  automatically.
- Neither present -> **stop** rather than silently generate a new
  `SECRET_KEY`/admin identity, since that could disconnect the application
  from its existing accounts and sessions. If this is genuinely a fresh
  installation, run `daily-figures install` instead.

`repair-installation` never touches the production database. Once it
reports the repaired paths, review the database candidates it lists and run
the explicit cutover:

```bash
sudo daily-figures repair-installation --yes
sudo daily-figures doctor --recovery          # optional: read-only re-check
sudo daily-figures migrate-storage --source /absolute/path/to/production.db
```

Run `sudo daily-figures doctor --recovery` at any time for a read-only
diagnostic of the current state file -- it reports unsafe storage paths and
any database/`.env` candidates found in known legacy locations without
selecting or copying anything, and works even when `install.conf` is
currently unsafe or invalid.
