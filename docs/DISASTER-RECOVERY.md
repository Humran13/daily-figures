# Disaster recovery

This starts with an empty replacement VPS and never needs the failed server.

1. Install CloudPanel on supported Ubuntu/Debian.
2. Recreate the exact Reverse Proxy site/SSL targeting `127.0.0.1:5000`.
3. Restore the CloudPanel site user's private home directories from the
   completed Drive archive so `app-data/daily-figures/production.db`, uploads,
   `app-config/daily-figures/.env`, and backups are already present.
4. Run the installer:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh
   ```

5. Verify deployment and database:

   ```bash
   sudo daily-figures doctor
   ```

6. Sign in over HTTPS and verify known users and records. Keep the archive and
   safety snapshot.

The installer automatically recognizes the restored database, preserves it,
creates a safety snapshot, applies pending migrations, and mounts that exact
private `app-data/daily-figures` directory at `/app/data`. If no records appear, stop immediately
and inspect the mount; do not enter new data until the intended database is
confirmed.

If the restored `install.conf` itself turns out to be stale/unsafe (for
example it still names a different or legacy storage layout), do not try to
fix it by hand: run `sudo daily-figures doctor --recovery` for a read-only
report, then `sudo daily-figures repair-installation` to repair it safely.
See [MIGRATION-FROM-OPT.md](MIGRATION-FROM-OPT.md).
