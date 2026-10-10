# Daily Figures

Production Flask inventory and daily-operations application, deployed behind a
CloudPanel reverse proxy with SQLite data stored outside the Git checkout and
outside Docker's writable layer.

## Quick installation

After installing CloudPanel and creating a **Reverse Proxy** site pointing to
`http://127.0.0.1:5000`, run this one line:

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh
```

The installer asks for the domain, mode, and fresh-install administrator
password. It discovers the real site user from the exact CloudPanel vhost and
owned `htdocs/<domain>` directory; it never constructs a username from the
domain or reads CloudPanel's internal database.

Interactive passwords must be at least 12 characters and use letters, numbers,
or `._~!@%+=,:/-`. For another valid Compose-env representation, supply a
carefully prepared `--env-file` in noninteractive mode.

Supported production hosts are current Ubuntu/Debian CloudPanel servers with
root/sudo, Git, Docker Engine/Compose v2, Python 3, `flock`, curl, at least
512 MiB RAM (1 GB recommended), and 1 GiB free on code/data filesystems.
Ordinary missing packages are installed on supported systems. Create the
CloudPanel site first.

Expected completion output includes:

```text
Installation: SUCCESS
Domain: figures.example.com
Port: 5000
Code: /opt/daily-figures
Data: /home/<actual-site-user>/app-data/daily-figures
Config: /home/<actual-site-user>/app-config/daily-figures
Backups: /home/<actual-site-user>/app-backups/daily-figures
Health: HEALTHY
```

For automation, prepare a root-readable mode-0600 environment file containing
`SECRET_KEY`, `SUPERADMIN_USERNAME`, and `SUPERADMIN_PASSWORD`, then:

```bash
sudo bash /tmp/daily-figures-install.sh --non-interactive --domain figures.example.com --mode fresh --port 5000 --env-file /root/daily-figures.env --backup-on-calendar '*-*-* 04:00:00' --retention 14
```

Noninteractive mode never prompts; required missing values fail clearly.

## Architecture and file storage

```text
/opt/daily-figures/                         versioned application code
/etc/daily-figures/install.conf             root-owned metadata (0600)
/home/<site-user>/
├── htdocs/<domain>/                        CloudPanel site marker/public root
├── app-data/daily-figures/
│   ├── production.db                       live SQLite database (0600)
│   └── uploads/                            persistent uploaded files
├── app-config/daily-figures/
│   └── .env                                runtime secrets (0600)
└── app-backups/daily-figures/
    ├── database-backups/                   verified scheduled/manual snapshots
    └── migration-backups/                  cutover recovery points
```

Only `/opt/daily-figures` is updated by Git. Production Compose requires an
explicit absolute data path and binds it to `/app/data`; it cannot silently use
the checkout's `data/`. Local development may intentionally use `./data`. The
app is published only on `127.0.0.1`. Its container runs with the actual
CloudPanel site UID/GID.

Container startup does not migrate or back up. Those are explicit, locked
management operations, so a Docker restart cannot unexpectedly change schema.
See [CloudPanel setup](docs/CLOUDPANEL-SETUP.md).

## Routine operations

```bash
sudo daily-figures update
sudo daily-figures backup
sudo daily-figures status
sudo daily-figures health
sudo daily-figures logs
sudo daily-figures restart
sudo daily-figures doctor
```

Update verifies the database and bind mount, makes a consistent online snapshot,
records old Git/image references, fast-forwards `main`, builds while the current
app serves, stops writes, makes a final cutover snapshot, migrates in a one-off
container, starts, and verifies mount/health. Update, restore, storage migration,
post-restore, and backup share a `flock` lock.

If migration starts and fails, the app remains stopped and the incomplete marker
is retained. Code rollback is not automatically claimed safe after schema
changes; inspect `/var/lib/daily-figures/update-in-progress` and use a verified
cutover snapshot when restoration is necessary.

See [Command reference](docs/COMMAND-REFERENCE.md).

## SQLite backups

```bash
sudo daily-figures backup
```

Python's SQLite online backup API reads a consistent view in WAL mode. It writes
a hidden `.partial` file under the site's normally excluded private `tmp/`
staging directory, runs `PRAGMA integrity_check`, calculates SHA-256, fsyncs,
atomically publishes `production-YYYYMMDD-HHMMSS.db`, writes `.sha256`, and only
then applies retention. It checks free space and exits nonzero on error.

```bash
journalctl -u daily-figures-backup.service
systemctl list-timers daily-figures-backup.timer
sudo daily-figures configure-backups --on-calendar '*-*-* 04:00:00' --retention 14
```

Systemd interprets the time in the VPS timezone shown by `timedatectl`. `04:00`
is an example intended to precede an example 05:00 CloudPanel remote job. Confirm
the actual CloudPanel schedule and leave an appropriate buffer.

## CloudPanel remote/Google Drive backups

Private directories live under the site home so CloudPanel can include them.
The app does not alter Rclone, OAuth credentials, an existing `remote:` custom
configuration, provider choice, or exclusions. Preserve a working Custom Rclone
Config for a personal Google account. Do not switch it to CloudPanel's built-in
Google Drive provider: CloudPanel currently documents that provider for paid
Google Workspace accounts, and this project does not create a new personal
Google OAuth integration.

CloudPanel normally archives a site's vhost and home while excluding `.ssh`,
`logs`, and `tmp`, which is why persistent data/config/backups are under the home
and temporary SQLite files are under `tmp`. Treat that as a default to verify,
not an assumption about the production exclusion list.

Run `sudo daily-figures backup-check`, then confirm in CloudPanel that the site
is selected and exclusions do not remove these directories. Download an archive,
inspect it, and verify a snapshot:

```bash
sudo daily-figures verify-db /path/to/downloaded/production-YYYYMMDD-HHMMSS.db
```

In Google Drive, open the configured `backups` directory, select the newest
completed site/server archive, and locate `app-data/daily-figures`,
`app-config/daily-figures`, and `app-backups/daily-figures`. Upload success alone
does not prove restorability. See [Backup and restore](docs/BACKUP-RESTORE.md).

## Existing `/opt` installation migration

Git updates never move storage. Prepare and run the dedicated cutover:

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh --mode existing
sudo daily-figures migrate-storage
```

It displays source/destination, verifies source, makes an online pre-cutover
snapshot, requires confirmation, stops writes, makes a final snapshot, refuses
an existing destination database, copies through SQLite, preserves uploads,
migrates explicitly, and verifies health/mount/data. The legacy database remains
for rollback. See [Migration from `/opt`](docs/MIGRATION-FROM-OPT.md).

## Complete VPS disaster recovery

1. Install CloudPanel on the new VPS.
2. Recreate the Reverse Proxy site/domain targeting `127.0.0.1:5000`.
3. Prepare without starting an empty application:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh --mode recovery
   ```

4. Download the chosen CloudPanel backup. Extract it only in private staging,
   inspect it, and restore the original `app-data/daily-figures`,
   `app-config/daily-figures`, and optionally `app-backups/daily-figures` to the
   printed paths. Never extract an unverified archive over a running install.
5. Activate:

   ```bash
   sudo daily-figures post-restore
   ```

`post-restore` requires `.env` and `production.db`, verifies integrity, repairs
ownership, makes a safety snapshot, builds, migrates once, starts, and checks the
exact mount and health. Recovery mode itself never creates a database, migrates,
or starts. See [Disaster recovery](docs/DISASTER-RECOVERY.md).

To restore one verified database:

```bash
sudo daily-figures restore --from /private/staging/production.db
```

This stages/verifies first, requires confirmation, takes safety snapshots, stops
writes, retains the old database as `production.pre-restore-*`, then migrates and
validates the restored application.

## CloudPanel configuration

Choose **Add Site → Create a Reverse Proxy**, enter the exact domain/site user,
and set `http://127.0.0.1:5000` (or the explicitly selected installer port).
Configure DNS and SSL. The installer validates exact vhost, target port, htdocs
ownership, Linux account, and home directory. It does not rewrite other sites.

## Troubleshooting

- **Port occupied:** `sudo ss -ltnp 'sport = :5000'`. Unrelated processes are
  never killed.
- **Site not found/wrong user:** confirm the exact Reverse Proxy site and that
  `/home/<user>/htdocs/<domain>` has the site's Linux owner.
- **HTTP 502:** run `status`, `health`, and `logs`; compare the CloudPanel port.
- **Docker failure:** use `sudo daily-figures logs --no-follow` and `docker info`.
- **Database missing/no records:** stop immediately; use `doctor` and inspect the
  `/app/data` mount. Never replace restored data with a new empty database.
- **Integrity failure:** keep stopped and select another verified snapshot.
- **Migration failure:** inspect logs and update marker; restore only through the
  confirmed managed workflow.
- **Permissions:** run `doctor`; never use `chmod 777`.
- **Backup failure:** inspect journald, `df -h`, timer status, and permissions.

## Security

`.env`, database, backups, and state use restrictive permissions; secrets are
not logged. Keep port 5000 on loopback, secure root/SSH and Google/Rclone access,
and never commit `.env`, databases, snapshots, uploads, tokens, or archives.
Because `.env` is under the site home, it may be included in CloudPanel remote
backups; the destination/account must be private and access-controlled.

## FAQ

**Database?** `/home/<site-user>/app-data/daily-figures/production.db`.

**Backups?** `/home/<site-user>/app-backups/daily-figures/database-backups/`.

**Can I reinstall/rebuild Docker without losing records?** Yes; data is an
external bind mount and existing databases are not replaced.

**What if Docker or the VPS is lost?** Files survive Docker removal. For total
loss, restore the CloudPanel archive on a new VPS and run `post-restore`.

**Restore from Google Drive?** Stage and inspect the downloaded CloudPanel
archive, restore private directories, then run `post-restore`.

**Change domain?** Create/validate the new CloudPanel site first; do not edit
`install.conf` manually.

**Update safely?** `daily-figures update` makes online and cutover recovery
points and leaves data outside Git/Docker.

## Development

```bash
cp .env.example .env
# fill in local secrets
docker compose up -d --build
```

Production always uses both Compose files through the management utility. No
test or installer contacts the production VPS.
