# Daily Figures

Daily Figures is a Flask inventory and daily-operations application deployed in
Docker behind a CloudPanel reverse proxy. Production listens on
`127.0.0.1:5000` and keeps persistent application data inside the matching
CloudPanel site user's private home directories so CloudPanel backups can
include it without exposing it beneath `htdocs`.

## One-line CloudPanel installation

First create the domain or subdomain in CloudPanel as a Reverse Proxy site that
points to `http://127.0.0.1:5000`. Then run:

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/daily-figures/main/install.sh -o /tmp/daily-figures-install.sh && sudo bash /tmp/daily-figures-install.sh
```

The installer asks exactly one question:

```text
Enter your CloudPanel domain or subdomain: stock.example.com
```

It does not ask for an installation mode, password, port, Docker path, backup
schedule, or retention. Port `5000`, daily SQLite snapshots at `04:00` server
time, and retention of 14 snapshots are automatic defaults. For automation,
the equivalent noninteractive form is:

```bash
sudo bash /tmp/daily-figures-install.sh --domain stock.example.com --non-interactive
```

The entered domain is validated against CloudPanel's enabled Nginx vhost. The
installer then finds exactly one owned `/home/<site-user>/htdocs/<domain>`
directory and verifies its real Linux owner. It never derives a username from
the domain and never hardcodes a site user or website path. If the site does not
exist, is ambiguous, is not a reverse proxy to port `5000`, or has unsafe
ownership, installation stops with an explanation.

Supported production hosts are current Ubuntu/Debian CloudPanel servers with
root/sudo and at least 512 MiB RAM and 1 GiB free disk space. Missing ordinary
prerequisites, including Docker Compose v2, are installed noninteractively.

## Persistent storage layout

For a domain such as `stock.example.com`, CloudPanel may own the site with any
valid site user. The discovered layout is:

```text
/opt/daily-figures/                              versioned application code
/etc/daily-figures/install.conf                  root-owned state (0600)
/home/<actual-site-user>/
├── htdocs/stock.example.com/                    CloudPanel site marker only
├── app-data/daily-figures/
│   ├── production.db                            live SQLite database (0600)
│   └── uploads/                                 persistent private uploads
├── app-config/daily-figures/
│   └── .env                                     runtime secrets (0600)
└── app-backups/daily-figures/
    ├── database-backups/                        verified SQLite snapshots
    └── migration-backups/                       explicit cutover snapshots
```

Production Compose requires the absolute configured `app-data` path and bind-mounts
it at `/app/data`. The application always uses
`/app/data/production.db`. There is no Docker named or anonymous volume for the
live database. Rebuilding or restarting the container therefore cannot replace
the private host copy of the database.

The database, secrets, uploads, and backups are never placed under `htdocs` or
inside the Git checkout. Their private filesystem locations and restrictive
permissions prevent direct HTTP access.

## Automatic fresh install, update, and recovery

The same command handles every installation state:

- If `app-data/daily-figures/production.db` exists, its integrity is checked and
  it is reused. Existing users, records, uploads, and configuration are kept.
- If a database exists under `/opt/daily-figures/data` or
  `htdocs/<domain>/data` while private storage is configured, installation
  stops and reports the path mismatch. It never copies or chooses a database.
- If no database exists, migrations initialize a new one.
- If `.env` is absent, it is generated automatically. If it exists, valid
  secrets and credentials are preserved and only missing values are appended.
- A fresh install generates an initial `admin` password and prints it once at
  completion. Change it immediately after signing in.
- A partial previous installation is safe to rerun only when its saved private
  paths agree with the discovered CloudPanel site. Valid saved paths are reused
  exactly; missing or conflicting databases stop the installer.

Before applying migrations to an existing or restored database, the installer
stops application writes and creates a verified snapshot under
`app-backups/daily-figures/database-backups`. The database is never overwritten
with an empty file.

## Restore on a new VPS

The intended CloudPanel/Google Drive recovery flow is:

1. Install CloudPanel on the new VPS.
2. Recreate the same Reverse Proxy site, targeting `127.0.0.1:5000`.
3. Restore that site user's private home directories from the existing
   CloudPanel backup so `app-data/daily-figures/production.db`,
   `app-data/daily-figures/uploads`, `app-config/daily-figures/.env`, and
   `app-backups/daily-figures`
   are present.
4. Run the one-line installer and enter the domain.

The installer discovers the restored site user, verifies the database,
repairs safe missing configuration, binds that exact `app-data` directory into
Docker, runs pending migrations, and starts the app. It does not require a
recovery mode.

## Replace the database manually

Do not copy over `production.db` while the application is running. Put the
candidate database in a private staging path and use the managed restore command:

```bash
sudo daily-figures restore --from /private/staging/production.db
```

The command verifies and stages the candidate first, asks for explicit restore
confirmation, obtains the deployment lock, snapshots the current database,
stops the application, retains the previous database as
`production.pre-restore-<timestamp>.db`, fixes ownership and permissions, runs
migrations, restarts, and checks health and the exact Docker mount. For reviewed
automation, add `--yes`.

## SQLite and CloudPanel backups

Create a transactionally consistent snapshot at any time:

```bash
sudo daily-figures backup
```

The scheduled and manual backup path is
`app-backups/daily-figures/database-backups`.
Snapshots use SQLite's online backup API, pass `PRAGMA integrity_check`, receive
a SHA-256 sidecar, are atomically published, and are pruned only after a new
snapshot succeeds.

The project does not create or modify a Google Drive/Rclone integration. Because
the live database, uploads, configuration, and snapshots are inside the
CloudPanel site user's home, they are available to the intended private-home
backup scope.
CloudPanel backup selections and provider-side exclusions cannot be proven from
the application installer. Check them with:

```bash
sudo daily-figures backup-check
```

Then confirm in CloudPanel that the domain is selected, no exclusion removes
`app-data`, `app-config`, or `app-backups`, and a downloaded archive contains
`production.db`, uploads, and at least one verified snapshot. Upload success
alone does not prove that a backup is restorable.

## Operations

```bash
sudo daily-figures status
sudo daily-figures health
sudo daily-figures logs
sudo daily-figures restart
sudo daily-figures update
sudo daily-figures backup
sudo daily-figures doctor
```

`update` checks the database and mount, creates online and stopped-write
snapshots, fast-forwards clean `main`, builds the image, applies migrations while
the app is stopped, and verifies health afterward. The backup, update, restore,
and migration paths share a filesystem lock.

## Development and tests

Local development may use the `./data` fallback in `docker-compose.yml`:

```bash
cp .env.example .env
# Fill in local-only values.
docker compose up -d --build
```

Production always adds `docker-compose.production.yml`, which requires explicit
absolute data and environment paths. Never use the bare development command in
production. Use `sudo daily-figures update`, `start`, or `restart`; those commands
load `/etc/daily-figures/install.conf` and the production Compose overlay.
Run tests with:

```bash
python -m pytest
```

No test or installer test contacts the production VPS.
