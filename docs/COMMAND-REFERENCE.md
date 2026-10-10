# Command reference

Production commands use `sudo` and `/etc/daily-figures/install.conf`.

| Command | Purpose |
|---|---|
| `daily-figures install` | Fresh/existing/recovery setup |
| `daily-figures update` | Backup, fast-forward, build, cutover, migrate, verify |
| `daily-figures backup` | Atomic verified SQLite snapshot |
| `daily-figures restore --from FILE` | Staged confirmed database replacement |
| `daily-figures migrate-storage` | Explicit legacy storage cutover |
| `daily-figures post-restore` | Validate and activate disaster restore |
| `daily-figures configure-backups` | Schedule and retention |
| `daily-figures backup-check` | CloudPanel scope verification checklist |
| `daily-figures status` | Paths and Compose status |
| `daily-figures start`, `stop`, `restart` | Lifecycle controls |
| `daily-figures logs [--no-follow]` | Application logs |
| `daily-figures health` | Local health endpoint |
| `daily-figures doctor` | Deployment diagnostics |
| `daily-figures verify-db [FILE]` | Integrity/hash report |

Install flags are `--domain`, `--mode fresh|existing|recovery`, `--port`,
`--env-file`, `--backup-on-calendar`, `--retention`, and `--non-interactive`.
Noninteractive requires domain/mode; fresh also requires an environment file.

Restore/migration require typing `RESTORE`; `--yes` is the explicit automation
equivalent. Conflicting operations share one lock. Scheduled backup skips with a
clear failure rather than racing a cutover. Commands never print `.env`.
