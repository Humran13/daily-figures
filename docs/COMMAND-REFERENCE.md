# Command reference

Production commands use `sudo` and `/etc/daily-figures/install.conf`.

| Command | Purpose |
|---|---|
| `daily-figures install` | Auto-detected fresh/existing/recovery setup |
| `daily-figures update` | Backup, fast-forward, build, cutover, migrate, verify |
| `daily-figures backup` | Atomic verified SQLite snapshot |
| `daily-figures restore --from FILE` | Staged confirmed database replacement |
| `daily-figures migrate-storage --source FILE` | Explicit legacy database cutover |
| `daily-figures repair-installation` | Repair a legacy/unsafe `install.conf` and `.env` |
| `daily-figures post-restore` | Validate and activate disaster restore |
| `daily-figures configure-backups` | Schedule and retention |
| `daily-figures backup-check` | CloudPanel scope verification checklist |
| `daily-figures status` | Paths, Compose status, next scheduled backup |
| `daily-figures start`, `stop`, `restart` | Lifecycle controls |
| `daily-figures logs [--no-follow]` | Application logs |
| `daily-figures health` | Local health endpoint |
| `daily-figures doctor [--recovery]` | Deployment diagnostics; `--recovery` is read-only and works on unsafe state |
| `daily-figures verify-db [FILE]` | Integrity/hash report |

`daily-figures COMMAND --help` works for every command above with **zero**
side effects: it never loads `install.conf`, checks the database or `.env`,
touches Docker, or requires root, even when the installation state is
missing or unsafe. This is intentional and tested -- see
[../docs/MIGRATION-FROM-OPT.md](MIGRATION-FROM-OPT.md) for the incident that
motivated it.

Install accepts only `--domain` and `--non-interactive`. With no flags it asks
only for the CloudPanel domain. Mode, port 5000, configuration repair, storage,
and backup defaults are automatic. Noninteractive installation requires the
domain. A pre-existing but unsafe/legacy `install.conf` is never silently
reused by `install`; it stops and points to `repair-installation`.

Every production lifecycle command loads `/etc/daily-figures/install.conf`,
validates the private CloudPanel paths, and uses both Compose files. Do not use
bare `docker compose up`, `build`, or `restart` commands in production:
`docker-compose.yml` requires `DAILY_FIGURES_DATA_DIR` to be set and has no
local-development fallback, so a bare invocation in a production checkout
fails before any container is created rather than silently binding the wrong
directory. Local development opts in explicitly with `docker-compose.dev.yml`
(see the main [README](../README.md)).
If a database exists in `/opt/daily-figures/data` or under `htdocs` while the
saved private path points elsewhere, commands stop without copying or creating
a database.

## Recovery commands

`daily-figures repair-installation [--yes]` is the only command allowed to
inspect an `install.conf` that fails normal validation (e.g. one still
pointing storage beneath `htdocs`, from before commit `443fc66`). It trusts
only scalar identity fields (domain, site user/home, code dir, port,
Compose file, backup schedule) from the existing file, never the legacy
`DATA_DIR`/`CONFIG_DIR`/`BACKUP_ROOT` values as destinations. It
re-validates the CloudPanel site, recomputes the required private
`app-data`/`app-config`/`app-backups` paths, backs up `install.conf`
(timestamped, never deleted) before writing the repair atomically, creates
the private directories, and recovers `.env` only when unambiguous. It never
touches the production database -- follow it with `migrate-storage`.

`daily-figures migrate-storage --source FILE [--yes] [--preserve-existing-destination]`
performs the actual database cutover. `--source` is mandatory and must be an
absolute path to a verified SQLite file; nothing is ever selected
automatically, even when only one legacy database is plausible. If the
destination already has a database, the command refuses to run unless
`--preserve-existing-destination` is given, in which case the existing
destination is archived under a timestamped name (never deleted) before the
explicit source is activated. Uploads are merged non-destructively: an empty
destination gets everything, identical same-path files are fine, a
differing same-path file stops the whole operation, and unique files on
either side are merged only when that is provably safe.

`daily-figures doctor --recovery` is a read-only diagnostic that works even
when `install.conf` is unsafe or invalid; it never mutates anything and never
prints secrets.

Restore/migration require typing `RESTORE`; `--yes` is the explicit automation
equivalent. Conflicting operations share one lock. Scheduled backup skips with a
clear failure rather than racing a cutover. Commands never print `.env`.
