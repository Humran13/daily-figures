# Daily Figures

Daily Figures is a Flask inventory and daily-operations application deployed in
Docker behind a CloudPanel reverse proxy. Production listens on
`127.0.0.1:5000` and keeps all persistent application data inside the matching
CloudPanel website directory.

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
/home/<actual-site-user>/htdocs/stock.example.com/
└── data/
    ├── .env                                     runtime secrets (0600)
    ├── production.db                            live SQLite database (0600)
    ├── uploads/                                 persistent private uploads
    └── backups/
        ├── production-YYYYMMDD-HHMMSS.db        consistent snapshots
        ├── production-....db.sha256             snapshot checksums
        └── migration/                           legacy cutover snapshots
```

Production Compose requires the absolute discovered `data` path and bind-mounts
it at `/app/data`. The application always uses
`/app/data/production.db`. There is no Docker named or anonymous volume for the
live database. Rebuilding or restarting the container therefore cannot replace
the website copy of the database.

The installer adds managed Nginx locations that return 404 for both `/data` and
`/data/`, validates the complete Nginx configuration, restores the original
vhost if validation fails, and reloads Nginx only after a successful check.
Filesystem permissions are also restrictive. This prevents direct HTTP access
to the database, uploads, backups, `.env`, and any alternate file route beneath
the website's `data` directory.

## Automatic fresh install, update, and recovery

The same command handles every installation state:

- If `htdocs/<domain>/data/production.db` exists, its integrity is checked and
  it is reused. Existing users, records, uploads, and configuration are kept.
- If the destination is empty but a legacy database exists under
  `/opt/daily-figures/data` or the previous `app-data/daily-figures` layout, the
  old app is stopped and SQLite's online backup API copies it only into the
  empty destination. The source remains untouched.
- If no database exists, migrations initialize a new one.
- If `.env` is absent, it is generated automatically. If it exists, valid
  secrets and credentials are preserved and only missing values are appended.
- A fresh install generates an initial `admin` password and prints it once at
  completion. Change it immediately after signing in.
- A partial previous installation is safe to rerun; completed data and valid
  configuration are reused.

Before applying migrations to an existing or restored database, the installer
stops application writes and creates a verified snapshot under `data/backups`.
The database is never overwritten with an empty file.

## Restore on a new VPS

The intended CloudPanel/Google Drive recovery flow is:

1. Install CloudPanel on the new VPS.
2. Recreate the same Reverse Proxy site, targeting `127.0.0.1:5000`.
3. Restore that website directory from the existing CloudPanel backup so
   `htdocs/<domain>/data/production.db`, uploads, backups, and preferably `.env`
   are present.
4. Run the one-line installer and enter the domain.

The installer discovers the restored website directory, verifies the database,
repairs safe missing configuration, binds that exact `data` directory into
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

The scheduled and manual backup path is the website's `data/backups` directory.
Snapshots use SQLite's online backup API, pass `PRAGMA integrity_check`, receive
a SHA-256 sidecar, are atomically published, and are pruned only after a new
snapshot succeeds.

The project does not create or modify a Google Drive/Rclone integration. Because
the live database, uploads, configuration, and snapshots are inside the website
directory, they are available to the existing CloudPanel website-backup scope.
CloudPanel backup selections and provider-side exclusions cannot be proven from
the application installer. Check them with:

```bash
sudo daily-figures backup-check
```

Then confirm in CloudPanel that the domain is selected, no exclusion removes
`htdocs/<domain>/data`, and a downloaded archive contains `production.db`,
uploads, and at least one verified snapshot. Upload success alone does not prove
that a backup is restorable.

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
absolute data and environment paths. Run tests with:

```bash
python -m pytest
```

No test or installer test contacts the production VPS.
