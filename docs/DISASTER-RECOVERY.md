# Disaster recovery

This starts with an empty replacement VPS and never needs the failed server.

1. Install CloudPanel on supported Ubuntu/Debian.
2. Recreate the exact Reverse Proxy site/SSL targeting `127.0.0.1:5000`.
3. Prepare without initializing data:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh --mode recovery
   ```

4. Download a completed CloudPanel/Drive archive into private staging. Inspect
   paths and extract only in staging.
5. Verify its database:

   ```bash
   sudo daily-figures verify-db /private/staging/.../production.db
   ```

6. Copy the staged `app-data/daily-figures` and
   `app-config/daily-figures` to the printed targets; restore uploads. Required
   files are `production.db` and `.env`. With the printed target paths, a typical
   staged copy is:

   ```bash
   sudo cp -a /private/staging/app-data/daily-figures/. /home/NEW_SITE_USER/app-data/daily-figures/
   sudo cp -a /private/staging/app-config/daily-figures/. /home/NEW_SITE_USER/app-config/daily-figures/
   ```

   Replace both source paths with the paths actually found in the downloaded
   archive and `NEW_SITE_USER` with the user printed by recovery preparation.
   Confirm the destinations before running either command.
7. Activate:

   ```bash
   sudo daily-figures post-restore
   sudo daily-figures doctor
   ```

8. Sign in over HTTPS and verify known users and records. Keep the archive and
   safety snapshot.

Recovery preparation never starts, creates an empty database, or migrates.
Post-restore refuses missing/corrupt files and snapshots before migration. If no
records appear, stop immediately and inspect the `/app/data` mount; do not enter
new data until the intended database is confirmed.
