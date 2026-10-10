# Backup and restore

## Snapshot guarantees

`sudo daily-figures backup` uses SQLite's online backup API, including in WAL
mode. A hidden `.partial` is written under the site's private `tmp/` staging
directory (normally excluded from CloudPanel archives), integrity-checked,
hashed, fsynced, and atomically renamed onto the same filesystem. Retention runs
only after success. Confirm the CloudPanel exclusion behavior during backup
verification rather than assuming defaults are unchanged.

```bash
systemctl list-timers daily-figures-backup.timer
journalctl -u daily-figures-backup.service
sudo daily-figures verify-db /private/staging/production-20261010-040000.db
sha256sum -c /private/staging/production-20261010-040000.db.sha256
```

The hash command applies when the metadata file was downloaded too.

## Restore one database

Never copy over an active database or carry old `-wal`/`-shm` files into a
restore.

```bash
sudo daily-figures restore --from /private/staging/production.db
```

The candidate is verified/staged first. After confirmation, the tool snapshots
the live database, stops writes, takes a final snapshot, retains old database and
sidecars under timestamped names, activates, fixes ownership, migrates, and
checks mount/health. `--yes` is the explicit noninteractive confirmation.

## CloudPanel/Google Drive verification

1. Run the local database snapshot before the CloudPanel job.
2. Confirm the intended site and private paths are in scope and no relevant
   exclusion exists. Do not alter exclusions without approval.
3. Confirm the remote job completes.
4. In the configured Drive `backups` directory, choose the latest completed
   server/site archive, not an in-progress object.
5. Download into private staging. List the archive and reject absolute paths,
   `..`, or unexpected links; never extract over live paths.
6. Confirm `app-data/daily-figures/production.db`, uploads,
   `app-config/daily-figures/.env`, and snapshots are present.
7. Verify the database, then use individual restore or the disaster runbook.

An upload success message alone does not prove restorability. Rehearse on a
disposable VPS periodically.
