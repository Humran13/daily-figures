#!/usr/bin/env python3
"""Inspect and conservatively remove provably stale Alembic SQLite temp tables."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


class GuardError(RuntimeError):
    pass


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_database(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise GuardError(f"database must be an existing regular file: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise GuardError("integrity_check failed: " + "; ".join(integrity))
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "alembic_version" not in tables:
            raise GuardError("alembic_version table is missing")
        versions = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
        if len(versions) != 1 or not versions[0]:
            raise GuardError("alembic_version must contain exactly one non-empty revision")
        findings = []
        for temp_name in sorted(name for name in tables if name.startswith("_alembic_tmp_")):
            original = temp_name.removeprefix("_alembic_tmp_")
            count = connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(temp_name)}"
            ).fetchone()[0]
            eligible = count == 0 and original in tables
            findings.append({
                "temporary_table": temp_name,
                "corresponding_table": original,
                "temporary_rows": count,
                "corresponding_table_exists": original in tables,
                "safe_stale_empty": eligible,
            })
    return {"database": str(path.resolve()), "integrity": "ok", "alembic_revision": versions[0], "temporary_tables": findings}


def cleanup(path: Path, report: dict) -> None:
    unsafe = [item for item in report["temporary_tables"] if not item["safe_stale_empty"]]
    if unsafe:
        raise GuardError("non-empty or ambiguous Alembic temporary table found; refusing cleanup")
    if not report["temporary_tables"]:
        return
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for item in report["temporary_tables"]:
            # Recheck after acquiring the write lock; never rely on stale inspection.
            table = item["temporary_table"]
            original = item["corresponding_table"]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if table not in tables:
                continue
            rows = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
            if rows != 0 or original not in tables:
                raise GuardError(f"state changed while checking {table}; refusing cleanup")
            connection.execute(f"DROP TABLE {quote_identifier(table)}")
        connection.commit()
    # A cleanup is successful only if the resulting file remains sound.
    after = inspect_database(path)
    if after["temporary_tables"]:
        raise GuardError("temporary tables remain after cleanup")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "cleanup"))
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = inspect_database(args.database)
        unsafe = [item for item in report["temporary_tables"] if not item["safe_stale_empty"]]
        if unsafe:
            raise GuardError("non-empty or ambiguous Alembic temporary table found; manual review required")
        if args.command == "cleanup":
            cleanup(args.database, report)
            report["cleaned"] = [item["temporary_table"] for item in report["temporary_tables"]]
        print(json.dumps(report, sort_keys=True))
    except (GuardError, OSError, sqlite3.Error) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
