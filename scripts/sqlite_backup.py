#!/usr/bin/env python3
"""Create and verify transactionally consistent SQLite snapshots."""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


class BackupError(RuntimeError):
    pass


def integrity_check(path: Path) -> None:
    if not path.is_file():
        raise BackupError(f"database does not exist: {path}")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
            rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        raise BackupError(f"cannot read SQLite database {path}: {exc}") from exc
    if rows != ["ok"]:
        raise BackupError(f"integrity_check failed for {path}: {'; '.join(rows)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _space_required(source: Path) -> int:
    committed_bytes = source.stat().st_size
    wal = source.with_name(source.name + "-wal")
    if wal.is_file():
        committed_bytes += wal.stat().st_size
    return max(committed_bytes * 2, 50 * 1024 * 1024)


def _unique_name(destination: Path, prefix: str) -> str:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    base = f"{prefix}-{stamp}"
    name = base
    counter = 0
    while (destination / f"{name}.db").exists():
        counter += 1
        name = f"{base}-{counter}"
    return name


def create_backup(
    source: Path,
    destination: Path,
    prefix: str,
    retention: int,
    temporary_directory: Path | None = None,
) -> Path:
    source = source.resolve()
    if not source.is_file():
        raise BackupError(f"database does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_directory = (temporary_directory or destination).resolve()
    temporary_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.stat().st_dev != temporary_directory.stat().st_dev:
        raise BackupError("temporary and backup directories must be on the same filesystem")
    try:
        os.chmod(destination, 0o700)
    except OSError:
        pass

    free = shutil.disk_usage(destination).free
    required = _space_required(source)
    if free < required:
        raise BackupError(
            f"insufficient free space in {destination}: need at least "
            f"{required} bytes, have {free} bytes"
        )

    name = _unique_name(destination, prefix)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".partial", dir=temporary_directory
    )
    os.close(fd)
    temporary = Path(temporary_name)
    published = destination / f"{name}.db"
    metadata = destination / f"{name}.db.sha256"
    metadata_temp = temporary_directory / f".{name}.sha256.partial"

    try:
        os.chmod(temporary, 0o600)
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary, timeout=30)) as dst:
                src.backup(dst, pages=1024, sleep=0.05)
                dst.commit()
        integrity_check(temporary)
        checksum = sha256(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, published)
        os.chmod(published, 0o600)
        metadata_temp.write_text(f"{checksum}  {published.name}\n", encoding="ascii")
        os.chmod(metadata_temp, 0o600)
        os.replace(metadata_temp, metadata)
        _fsync_directory(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
        published.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise

    if retention > 0:
        backups = sorted(
            destination.glob(f"{prefix}-*.db"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old in backups[retention:]:
            old.unlink()
            old.with_name(old.name + ".sha256").unlink(missing_ok=True)

    print(json.dumps({
        "backup": str(published),
        "bytes": published.stat().st_size,
        "sha256": checksum,
        "integrity": "ok",
    }))
    return published


def copy_database(source: Path, destination: Path) -> None:
    """Copy a database through SQLite into a new, atomically published file."""
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise BackupError(f"refusing to overwrite existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary, timeout=30)) as dst:
                src.backup(dst, pages=1024, sleep=0.05)
                dst.commit()
        integrity_check(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    print(json.dumps({"database": str(destination), "integrity": "ok"}))


def verify_backup(path: Path, checksum_file: Path | None = None) -> None:
    integrity_check(path)
    checksum = sha256(path)
    if checksum_file:
        expected = checksum_file.read_text(encoding="ascii").split()[0]
        if checksum != expected:
            raise BackupError(f"SHA-256 mismatch for {path}")
    print(json.dumps({"database": str(path.resolve()), "sha256": checksum, "integrity": "ok"}))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--prefix", default="production")
    create.add_argument("--retention", type=int, default=14)
    create.add_argument("--temporary-directory", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--checksum-file", type=Path)
    copy = sub.add_parser("copy")
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--destination-file", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "create":
            if args.retention < 1:
                raise BackupError("retention must be at least 1")
            create_backup(
                args.source,
                args.destination,
                args.prefix,
                args.retention,
                args.temporary_directory,
            )
        elif args.command == "verify":
            verify_backup(args.database, args.checksum_file)
        else:
            copy_database(args.source, args.destination_file)
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
