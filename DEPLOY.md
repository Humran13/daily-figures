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
2. SSH into your server and edit `docker-compose.yml`:
   - Change `APP_PIN` to a real PIN your clerk will use.
   - Change `SECRET_KEY` to a random string (e.g. run `openssl rand -hex 32`).
   - Change the host port (`"5000:5000"`) if 5000 is already in use.
3. Build and start it:
   ```bash
   cd /home/youruser/daily-figures
   docker compose up -d --build
   ```
4. Visit `http://your-server-ip:5000` — you should see the PIN screen.
5. **Put it behind HTTPS.** Don't expose port 5000 directly to the internet
   long-term. Easiest option if you're already using CloudPanel: create a
   "Reverse Proxy" site in CloudPanel pointing at `127.0.0.1:5000`, and let
   CloudPanel issue a free Let's Encrypt SSL certificate for it. Then the
   clerk visits `https://figures.yourdomain.com` instead of a bare IP.

## Backing up your data

The entire database is one file: `./data/production.db`. Back it up with:
```bash
cp data/production.db data/production_backup_$(date +%Y%m%d).db
```
Consider a nightly cron job that copies this file somewhere safe (or emails
it to yourself).

## Updating the app later

```bash
cd /home/youruser/daily-figures
docker compose down
docker compose up -d --build
```
Your data in `./data` is untouched by this — it only rebuilds the app code.

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
