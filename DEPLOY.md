# Daily Figures deployment

The production deployment guide is now [README.md](README.md).

Detailed runbooks:

- [CloudPanel setup](docs/CLOUDPANEL-SETUP.md)
- [Backup and restore](docs/BACKUP-RESTORE.md)
- [Disaster recovery](docs/DISASTER-RECOVERY.md)
- [Migration from `/opt`](docs/MIGRATION-FROM-OPT.md)
- [Management command reference](docs/COMMAND-REFERENCE.md)

The old container-entrypoint migration and live-file-copy backup workflow has
been retired. Use `sudo daily-figures ...` commands so operations are locked,
SQLite-consistent, verified, and use the configured private data path.
