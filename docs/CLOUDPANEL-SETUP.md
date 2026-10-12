# CloudPanel setup

References: [CloudPanel Add Site](https://www.cloudpanel.io/docs/v2/frontend-area/add-site/)
and [CloudPanel root CLI commands](https://www.cloudpanel.io/docs/v2/cloudpanel-cli/root-user-commands/).

1. Point DNS at the VPS.
2. In CloudPanel choose **Add Site → Create a Reverse Proxy**.
3. Enter the exact domain/site user and `http://127.0.0.1:5000`.
4. Issue SSL and confirm HTTPS.
5. Run the README one-line installer.

The installer reads the enabled vhost and owned
`/home/*/htdocs/<exact-domain>` directory. It rejects ambiguous ownership, a
missing account, non-reverse-proxy config, and a different port. It never reads
CloudPanel's internal database. It adds an idempotent deny rule for `/data`,
validates Nginx, and rolls the vhost back if validation fails.

Production always uses port 5000 and binds only loopback. It never kills an
unrelated listener.

```bash
sudo ss -ltnp 'sport = :5000'
curl -fsS http://127.0.0.1:5000/api/health
sudo daily-figures doctor
```

Keep a working Custom Rclone Config (`remote:`) and credentials unchanged.
CloudPanel's built-in Google Drive provider is documented for paid Google
Workspace; this project neither switches providers nor creates a new personal
Google OAuth connection.
Run `sudo daily-figures backup-check`, then confirm in CloudPanel that the site
and its `htdocs/<domain>/data` directory are included and no exclusion matches it. Confirm the
remote schedule follows the local SQLite timer. The installer reports the VPS
timezone; 05:00 is an example, not a hardcoded assumption.
