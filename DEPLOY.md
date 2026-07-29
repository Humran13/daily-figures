# Deploying Daily Figures to your server

## Why Docker over a native CloudPanel site

CloudPanel is great for PHP/Node "sites," but this app is a small custom
Python service with its own database file. Docker is the better fit because:
- **One command to deploy or move it** — `docker compose up -d` works
  identically on any server, so if you ever migrate hosts, you just copy
  the folder over.
- **The database persists safely** — the SQLite file lives in `./data` on
  your host machine (outside the container), so restarting/rebuilding the
  app never touches your production data.
- **No Python/pip version conflicts** with anything else running on your
  server — it's fully isolated.

If your server doesn't have Docker yet, most providers let you install it
with:
```bash
curl -fsSL https://get.docker.com | sh
```

## Deploy with Docker (recommended)

1. Copy the whole `webapp_server` folder to your server, e.g. via `scp`:
   ```bash
   scp -r webapp_server youruser@yourserver:/home/youruser/daily-figures
   ```
2. SSH into your server and set up your secrets — **never edit
   docker-compose.yml with real secrets; it's tracked in git.**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` (which is git-ignored) and fill in:
   - `SECRET_KEY` — required, the app refuses to start without it. Generate
     one with `openssl rand -hex 32`.
   - `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` — only take effect once,
     on the very first startup when the `users` table is empty, to create
     your first login. Safe to leave in `.env` afterwards; they're ignored
     once any user exists.
   - Change the host-side port in `docker-compose.yml` if `127.0.0.1:5000`
     collides with something else already running — but keep the
     `127.0.0.1:` prefix. The app is meant to sit behind CloudPanel's
     reverse proxy, never exposed directly to the internet on any port.
3. Build and start it:
   ```bash
   cd /home/youruser/daily-figures
   docker compose up -d --build
   ```
   The container's entrypoint (`docker-entrypoint.sh`) runs `flask db
   upgrade` before gunicorn starts, so schema migrations apply
   automatically on every deploy — see "Database migrations" below.
4. The container's port is bound to `127.0.0.1:5000` — it is **not**
   reachable from outside the server at all (by design; this is the
   production-safety fix that keeps a bare Flask/Gunicorn port from ever
   facing the internet directly). From the server itself,
   `curl http://127.0.0.1:5000/api/health` should return `{"status":"ok"}`.
5. **Put it behind CloudPanel's reverse proxy for real access.** Create a
   "Reverse Proxy" site in CloudPanel pointing at `127.0.0.1:5000`, and let
   CloudPanel issue a free Let's Encrypt SSL certificate for it. Then visit
   `https://figures.yourdomain.com` — you should see the sign-in screen.
   Log in with the `SUPERADMIN_USERNAME`/`SUPERADMIN_PASSWORD` you set, then
   create individual accounts for everyone else from `/admin.html`.

## Backing up your data

The database is one file: `./data/production.db`. Uploaded company
branding assets (the logo, if one has been set) live under
`./data/uploads/branding/` — same persistent volume, not baked into the
image, so both survive container rebuilds identically.

**Database backup happens automatically on every deploy/restart.**
`docker-entrypoint.sh` runs `scripts/backup_db.sh` before touching the
schema at all — it copies `production.db` to
`./data/backups/production_<timestamp>.db`, and if that copy fails for any
reason (disk full, permissions, whatever), the entrypoint aborts
immediately and **`flask db upgrade` never runs**. The container will
refuse to start rather than migrate an unbacked-up database. It never
overwrites an existing backup file — a same-second collision gets a
numeric suffix instead.

To back up by hand (e.g. before poking at something manually, or on a
schedule outside of a deploy):
```bash
sh scripts/backup_db.sh ./data/production.db
```
Consider also pointing a nightly cron job at this script, or copying the
whole `./data/` directory (database backups **and** `uploads/branding/`)
somewhere off-server.

**Restoring from a backup:**
```bash
docker compose down
cp data/backups/production_<timestamp>.db data/production.db
docker compose up -d
```
Restoring the database does not by itself restore a logo file — if you
also need the exact logo that was live at backup time, copy your own
off-server copy of `data/uploads/branding/` back into place before
starting the container. A missing logo file is not fatal either way: the
app falls back to a plain text company name automatically.

## Updating the app later

```bash
cd /home/youruser/daily-figures
docker compose down
docker compose up -d --build
```
Your data in `./data` is untouched by this — it only rebuilds the app code.
The entrypoint runs any new migrations automatically on startup.

## Database migrations

Schema changes ship as versioned scripts in `migrations/versions/`, applied
with Alembic via Flask-Migrate. They are **additive only** — no migration in
this project ever alters or drops the original `entries` table (the Daily
Figures data), and this is enforced by `migrations/env.py`'s
`include_object` filter plus a test (`tests/test_migrations.py`) that scans
every migration script for `drop_table('entries')` and fails the suite if
one is ever added.

**Applying migrations** happens automatically via `docker-entrypoint.sh` on
every container start — immediately after the automatic backup described
above. To run it by hand instead (e.g. outside Docker), back up first,
then:
```bash
sh scripts/backup_db.sh /path/to/production.db
export FLASK_APP=app.py
export DB_PATH=/path/to/production.db
flask db upgrade
```

**Rolling back** a migration:
```bash
flask db downgrade <revision-id>   # or: flask db downgrade -1
```
If anything looks wrong afterwards, restoring the pre-migration backup is
always the fastest safety net — see "Restoring from a backup" above.

Current migrations and what their rollback does:
- `a451e04281fc` (initial: users, products, packaging_rules, customers,
  audit_log) — downgrade drops exactly those 5 new tables. `entries` is
  never touched in either direction.
- `a939d0b27a3e` (seed default products and packaging rules) — downgrade
  deletes only the specific seeded product/rule rows it inserted (matched
  by name), leaving any products you've added since untouched.
- `64984c2b0aba`, `b4961f69011b`, `b10744abb49e` — additive dispatch/daily-
  figures/low-stock-threshold tables and columns; downgrades drop exactly
  what each one added.
- `012e556ab7ad` (sales categories + recipient fields) — adds the
  `sales_categories` table (seeded with the 5 fixed categories) and
  nullable columns on `customers`/`dispatches`. Downgrade drops exactly
  those; `customers` and `dispatches` themselves are never touched.
- `135b08c5ab45` (customer normalized_name) — adds a case/whitespace-
  insensitive comparison column used for duplicate detection, backfilled
  for every existing customer row. Attempts a UNIQUE index; if your data
  already has case/whitespace-only duplicate names, it falls back to a
  non-unique index and prints a warning rather than failing the deploy —
  check the container logs after upgrading for that warning and resolve
  any flagged duplicates by hand if it appears.
- `2a904d1ebe3b` (operator_daily_figure_permissions) — adds a single
  role-wide settings row (seeded all-`False`). Downgrade drops exactly
  that table.
- `8d16f14e2b4a` (company_settings) — adds a single-row white-label
  branding/configuration table, seeded with `display_name="Daily Figures"`
  and every other field blank. Downgrade drops exactly that table; it does
  not touch or delete any uploaded logo file under
  `data/uploads/branding/` (those aren't tracked by the database at all —
  see "Backing up your data" above).

**Always back up `data/production.db` before running a migration against
real production data** — as of this version that happens automatically,
but a manual `sh scripts/backup_db.sh data/production.db` right before a
risky change costs two seconds and never hurts.

## Migrating legacy Daily Figures data (one-time, manual)

Phase 4 replaced hand-typed Issued with an auto-calculated total derived
from finalized dispatches, and moved Opening/Return/Production to exact
cartons/packs/pieces instead of the old decimal notation. The original
`entries` table is preserved forever and is never touched by any of this.

To bring old entries into the new structure: log in as a Super
Administrator, open `/admin.html` → **Legacy Data** → **Run migration
now**. It's safe to run more than once (already-migrated rows are
skipped). Anything it can't confidently decode — a product whose
packaging ratio doesn't fit the old single-digit-per-unit notation, or an
out-of-range value — is listed under "Flagged for manual review" instead
of being guessed; re-enter those specific date/shift/product rows by hand
via the Enter tab once you know the correct figures.

## Accounts and roles

The old shared PIN is gone. There are 4 roles — Super Administrator,
Manager, Operator, Viewer — described in the Phase 1 analysis. The first
Super Administrator account is created automatically from the
`SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` environment variables the
first time the app starts with an empty `users` table; from then on, manage
everyone else from `/admin.html` (Super Administrator only).

## Health checks and post-deployment verification

The container has a built-in Docker `HEALTHCHECK` that hits
`GET /api/health` (unauthenticated, checks the process is up and the
SQLite file is actually reachable) every 30s. Check it with:
```bash
docker inspect --format='{{json .State.Health}}' daily_figures_app
```
or just `docker ps` — unhealthy containers show `(unhealthy)` next to the
uptime.

After every deploy, verify:
1. `curl http://127.0.0.1:5000/api/health` → `{"status":"ok"}`.
2. Sign in at `/` with a real account (not the seed super-admin, if you've
   since created named accounts).
3. Open `/dashboard.html` — confirms the daily-figures, dispatch, and
   customer subsystems are all reachable end-to-end.
4. Create one draft dispatch and finalize it (`/dispatch.html`), then
   confirm its Issued total shows up on `/` under the matching
   date/shift/product — this is the one thing that must never silently
   break, since it's the core "no double entry" guarantee of the whole
   rebuild.
5. Check `docker compose logs -f daily-figures` for migration errors —
   the entrypoint runs `flask db upgrade` before gunicorn starts, and logs
   there if a migration fails (the container won't come up in that case).

## Connecting this to the daily/monthly reports

Once the clerk is using this app instead of the spreadsheet, export the CSV
(History tab → "Export CSV") and feed it into `monthly_report.py` /
`daily_report.py` from the earlier kit — or, once you're ready, I can wire
those scripts to pull directly from this app's `/api/export.csv` endpoint
automatically, so no manual export step is needed at all.

## If you'd rather use CloudPanel's native Python/Node site type instead

CloudPanel does support "Python" sites directly (via its site wizard). That
works too, but you'd be running Flask under CloudPanel's own process
manager rather than Docker, so app updates mean re-uploading files rather
than one `docker compose` command, and you lose the "runs identically
anywhere" portability. If you'd prefer that route instead, let me know and
I'll adjust the setup for it (mainly: no Dockerfile needed, just a WSGI
entry point CloudPanel can point to).
