"""
These tests exercise the real Alembic migration scripts in migrations/
against a throwaway sqlite file — not db.create_all() — because the whole
point is to prove the migrations themselves are safe to run against a
database that already has the legacy `entries` table and production rows
in it, the way the real server's database does.
"""
import sqlite3

import pytest


@pytest.fixture
def bare_app(tmp_path, monkeypatch):
    """Like the `app` fixture, but does NOT call db.create_all() — the
    migration scripts are responsible for creating everything here."""
    db_path = tmp_path / "migration_test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)
    return db_path


def _seed_legacy_entries_table(db_path):
    """Simulate a real production database: the entries table already
    exists with real rows before this migration suite ever ran."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, shift TEXT NOT NULL, product TEXT NOT NULL,
            opening REAL NOT NULL, return_val REAL NOT NULL DEFAULT 0,
            production REAL NOT NULL DEFAULT 0, issued REAL NOT NULL DEFAULT 0,
            closing REAL NOT NULL, notes TEXT DEFAULT '', updated_at TEXT NOT NULL,
            UNIQUE(date, shift, product)
        )
    """)
    conn.execute("""
        INSERT INTO entries (date, shift, product, opening, return_val, production, issued, closing, notes, updated_at)
        VALUES ('2026-07-01','Day','Compact Corporate',100,0,50,20,130,'','2026-07-01T00:00:00')
    """)
    conn.commit()
    conn.close()


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return names


def test_upgrade_preserves_existing_entries_and_seeds_products(bare_app):
    db_path = bare_app
    _seed_legacy_entries_table(db_path)

    from webapp import create_app
    from flask_migrate import upgrade

    flask_app = create_app()  # runs init_legacy_db — must be a harmless no-op here
    with flask_app.app_context():
        upgrade()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    entries_rows = conn.execute("SELECT * FROM entries").fetchall()
    assert len(entries_rows) == 1
    assert entries_rows[0]["product"] == "Compact Corporate"
    assert entries_rows[0]["closing"] == 130

    product_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert product_count == 24

    rule_count = conn.execute("SELECT COUNT(*) FROM packaging_rules").fetchone()[0]
    assert rule_count == 22  # 24 products minus Kantimba and Kitchen Towel (no defined rule)

    kitchen_towel = conn.execute("SELECT id FROM products WHERE name='Kitchen Towel'").fetchone()[0]
    doubles_parent = conn.execute(
        "SELECT parent_product_id FROM products WHERE name='Kitchen Towel Doubles'"
    ).fetchone()[0]
    assert doubles_parent == kitchen_towel
    conn.close()


def test_upgrade_is_reentrant(bare_app):
    db_path = bare_app
    _seed_legacy_entries_table(db_path)

    from webapp import create_app
    from flask_migrate import upgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade()
        upgrade()  # must not error or duplicate seed rows

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 24
    conn.close()


def test_downgrade_to_base_removes_new_tables_but_keeps_entries(bare_app):
    db_path = bare_app
    _seed_legacy_entries_table(db_path)

    from webapp import create_app
    from flask_migrate import upgrade, downgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade()
        downgrade(revision="base")

    remaining = _tables(db_path)
    assert "entries" in remaining
    for new_table in ("products", "users", "customers", "packaging_rules", "audit_log"):
        assert new_table not in remaining

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT * FROM entries").fetchall()[0][3] == "Compact Corporate"
    conn.close()


def test_no_single_migration_ever_drops_entries():
    """Static guard: scan every migration script and fail loudly if one is
    ever added that touches the entries table."""
    import pathlib
    versions_dir = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"
    for script in versions_dir.glob("*.py"):
        text = script.read_text()
        assert "drop_table('entries')" not in text, f"{script.name} must never drop the entries table"
        assert 'drop_table("entries")' not in text, f"{script.name} must never drop the entries table"
