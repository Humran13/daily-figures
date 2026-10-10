# Migrate storage from `/opt`

Source: `/opt/daily-figures/data/production.db`

Target: `/home/<actual-site-user>/app-data/daily-figures/production.db`

## Preflight and cutover

Confirm health/free space and create the correct CloudPanel Reverse Proxy site.

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh --mode existing
sudo daily-figures migrate-storage
```

Read the plan and type `RESTORE`. The tool verifies source, makes an online
pre-cutover snapshot, stops legacy writes, makes a final snapshot, refuses any
existing destination DB, copies through SQLite, copies uploads only to an empty
target, fixes ownership, migrates once, starts, and verifies integrity/mount/
health. Original data and migration snapshots remain intact.

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
