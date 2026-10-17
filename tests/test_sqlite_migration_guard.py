"""Safety tests for scripts/sqlite_migration_guard.py.

The guard is deliberately conservative: it may only remove an empty
``_alembic_tmp_*`` table when the real table still exists, and it must keep a
database with any ambiguous/non-empty temp table untouched.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


GUARD = Path(__file__).resolve().parents[1] / "scripts" / "sqlite_migration_guard.py"


def _connect(path):
    return sqlite3.connect(path)


def _make_database(path, *, temp_rows=0, create_original=True, revision="abc123"):
    connection = _connect(path)
    try:
        with connection:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            if revision is not None:
                connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
            if create_original:
                connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
                connection.execute("INSERT INTO items (name) VALUES ('keep me')")
            connection.execute("CREATE TABLE _alembic_tmp_items (id INTEGER PRIMARY KEY, name TEXT)")
            for index in range(temp_rows):
                connection.execute("INSERT INTO _alembic_tmp_items (name) VALUES (?)", (f"row {index}",))
    finally:
        connection.close()


def _run(command, database):
    return subprocess.run(
        [sys.executable, str(GUARD), command, "--database", str(database)],
        capture_output=True, text=True, check=False,
    )


def _tables(path):
    connection = _connect(path)
    try:
        return {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()


def test_empty_orphan_temp_table_is_reported_and_removed(tmp_path):
    database = tmp_path / "safe.db"
    _make_database(database)

    inspected = _run("inspect", database)
    assert inspected.returncode == 0, inspected.stderr
    report = json.loads(inspected.stdout)
    assert report["integrity"] == "ok"
    assert report["alembic_revision"] == "abc123"
    assert report["temporary_tables"] == [{
        "temporary_table": "_alembic_tmp_items",
        "corresponding_table": "items",
        "temporary_rows": 0,
        "corresponding_table_exists": True,
        "safe_stale_empty": True,
    }]

    cleaned = _run("cleanup", database)
    assert cleaned.returncode == 0, cleaned.stderr
    assert json.loads(cleaned.stdout)["cleaned"] == ["_alembic_tmp_items"]
    assert "_alembic_tmp_items" not in _tables(database)
    assert "items" in _tables(database)

    connection = _connect(database)
    try:
        assert connection.execute("SELECT name FROM items").fetchall() == [("keep me",)]
    finally:
        connection.close()


def test_non_empty_temp_table_is_never_removed(tmp_path):
    database = tmp_path / "non-empty.db"
    _make_database(database, temp_rows=1)

    inspected = _run("inspect", database)
    assert inspected.returncode == 1
    assert "non-empty or ambiguous" in inspected.stderr

    cleaned = _run("cleanup", database)
    assert cleaned.returncode == 1
    assert "_alembic_tmp_items" in _tables(database)


def test_temp_table_without_original_table_is_ambiguous(tmp_path):
    database = tmp_path / "ambiguous.db"
    _make_database(database, create_original=False)

    result = _run("cleanup", database)
    assert result.returncode == 1
    assert "non-empty or ambiguous" in result.stderr
    assert "_alembic_tmp_items" in _tables(database)


def test_missing_or_invalid_alembic_version_is_rejected(tmp_path):
    missing = tmp_path / "missing-version.db"
    _make_database(missing, revision=None)
    result = _run("inspect", missing)
    assert result.returncode == 1
    assert "alembic_version must contain exactly one" in result.stderr

    invalid = tmp_path / "invalid-version.db"
    _make_database(invalid, revision="")
    result = _run("inspect", invalid)
    assert result.returncode == 1
    assert "alembic_version must contain exactly one" in result.stderr


def test_missing_database_is_rejected(tmp_path):
    result = _run("inspect", tmp_path / "does-not-exist.db")
    assert result.returncode == 1
    assert "existing regular file" in result.stderr
