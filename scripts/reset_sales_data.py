#!/usr/bin/env python3
"""Reset the Sales Department's transactional data to a genuine zero state.

Testing the Sales workflow leaves rows that look like real accounting history
afterwards.  This script removes that transaction layer - sales days, counted
vehicle stock, sales cards, expenses, money movements, debts and starting
points - and leaves every master record alone (products, packaging rules, sales
categories, customers, users, sales profiles, permissions, settings, the audit
trail, the schema and the Alembic history).

It is deliberately awkward to fire by accident:

  * it does nothing at all without ``--apply``;
  * ``--apply`` additionally requires ``--confirm RESET-SALES-DATA``;
  * before deleting anything it writes a verified backup next to the database
    (``<db>.bak-<timestamp>``) unless ``--no-backup`` is given;
  * the deletions run in one transaction and are rolled back on any error;
  * it prints what was deleted and what was preserved, and re-reads the
    database afterwards to prove the Sales tables are empty.

Usage:

    # see exactly what would be deleted (default: read-only)
    python scripts/reset_sales_data.py --database data/production.db

    # take the backup and reset
    python scripts/reset_sales_data.py --database data/production.db \
        --apply --confirm RESET-SALES-DATA

DB_PATH is used when ``--database`` is not given.  The app is never deployed,
restarted or migrated by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

CONFIRM_TEXT = "RESET-SALES-DATA"
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _backup(database: Path) -> Path:
    """A transactionally consistent copy, verified before anything is deleted."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = database.with_name(f"{database.name}.bak-{stamp}")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as source:
        with sqlite3.connect(str(target)) as destination:
            source.backup(destination)
    with sqlite3.connect(str(target)) as check:
        problems = [row[0] for row in check.execute("PRAGMA integrity_check")]
    if problems != ["ok"]:
        raise RuntimeError(f"backup failed its integrity check: {problems}")
    return target


def _counts():
    from webapp.services import sales_reset_service

    return sales_reset_service.sales_row_counts(), sales_reset_service.preserved_row_counts()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", help="SQLite database to reset (defaults to DB_PATH)")
    parser.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    parser.add_argument("--confirm", help=f"required with --apply: {CONFIRM_TEXT}")
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-reset backup")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    database = args.database or os.environ.get("DB_PATH")
    if not database:
        print("error: give --database or set DB_PATH", file=sys.stderr)
        return 1
    # Resolve before handing it to the app: Flask-SQLAlchemy resolves a *relative*
    # sqlite URL against the instance folder, which would silently open (or
    # create) a different file than the one the operator named.
    path = Path(database).resolve()
    if not path.is_file():
        print(f"error: database does not exist: {path}", file=sys.stderr)
        return 1
    if args.apply and (args.confirm or "") != CONFIRM_TEXT:
        print(f"error: --apply also needs --confirm {CONFIRM_TEXT}", file=sys.stderr)
        return 2

    # create_app() reads DB_PATH, so point it at the target before importing it.
    os.environ["DB_PATH"] = str(path)
    os.environ.setdefault("SECRET_KEY", "sales-reset")

    from webapp import create_app
    from webapp.extensions import db
    from webapp.services import sales_reset_service

    app = create_app()
    with app.app_context():
        planned, preserved = _counts()
        if not planned:
            print(f"No Sales tables found in {path}; nothing to reset.", file=sys.stderr)
            return 1
        report = {"database": str(path), "applied": False,
                  "would_delete": planned, "preserved_before": preserved}
        if not args.apply:
            report["dry_run"] = True
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"DRY RUN - nothing was deleted in {path}")
                print("\nSales transaction rows that would be removed:")
                for name, count in planned.items():
                    print(f"  {name:<32} {count:>6}")
                print(f"  {'TOTAL':<32} {sum(planned.values()):>6}")
                print("\nPreserved master data:")
                for name, count in preserved.items():
                    print(f"  {name:<32} {count:>6}")
                print(f"\nRun again with --apply --confirm {CONFIRM_TEXT} to reset.")
            return 0

        backup = None if args.no_backup else _backup(path)
        try:
            result = sales_reset_service.reset_sales_transactions()
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            print(f"error: reset failed and was rolled back: {exc}", file=sys.stderr)
            if backup:
                print(f"the pre-reset backup is intact at {backup}", file=sys.stderr)
            return 1
        verified = sales_reset_service.verify_sales_reset()

    report.update({"applied": True, "backup": str(backup) if backup else None,
                   "deleted": result["deleted"], "preserved_after": verified["kept"],
                   "verified_empty": verified["empty"], "remaining": verified["remaining"]})
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Sales transactional data reset in {path}")
        print(f"backup: {backup if backup else 'skipped (--no-backup)'}\n")
        print("Rows removed:")
        for name, count in result["deleted"].items():
            print(f"  {name:<32} {count:>6}")
        print(f"  {'TOTAL':<32} {sum(result['deleted'].values()):>6}")
        print("\nPreserved master data (unchanged):")
        for name, count in verified["kept"].items():
            print(f"  {name:<32} {count:>6}")
        print("\nSales tables now empty:", "yes" if verified["empty"] else f"NO - {verified['remaining']}")
    return 0 if verified["empty"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
