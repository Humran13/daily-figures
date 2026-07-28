"""
Proves the sales-category migration (012e556ab7ad) is safe against a
database that already has real customers/dispatches rows from BEFORE this
enhancement — same technique as tests/test_migrations.py: build the schema
up to the prior revision, insert rows using that exact (older) shape, then
upgrade to head and check nothing was lost, and downgrade cleanly removes
only what this migration added.
"""
import sqlite3

import pytest


@pytest.fixture
def bare_app(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)
    return db_path


PRIOR_REVISION = "b10744abb49e"  # add_low_stock_threshold_to_products — right before sales categories
HEAD_REVISION = "012e556ab7ad"  # add_sales_categories_and_recipient_fields


def _seed_pre_enhancement_data(db_path):
    """Insert a customer + dispatch using the table shape as it existed
    immediately before this migration (no sales_category_id/is_temporary/
    merged_into_id/snapshot columns)."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO customers (name, category, active, created_at, updated_at)
        VALUES ('Legacy Customer', 'customer', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """)
    customer_id = conn.execute("SELECT id FROM customers WHERE name='Legacy Customer'").fetchone()[0]

    conn.execute("""
        INSERT INTO products (name, display_order, active, created_at, updated_at)
        VALUES ('Legacy Product', 1, 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """)
    product_id = conn.execute("SELECT id FROM products WHERE name='Legacy Product'").fetchone()[0]
    conn.execute("""
        INSERT INTO packaging_rules (product_id, cartons_to_packs, packs_to_pieces, effective_from, created_at)
        VALUES (?, 10, 10, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """, (product_id,))
    rule_id = conn.execute("SELECT id FROM packaging_rules WHERE product_id=?", (product_id,)).fetchone()[0]

    conn.execute("""
        INSERT INTO dispatches (dispatch_number, date, shift, customer_id, status,
                                 duplicate_override, created_at, updated_at)
        VALUES ('LEGACY-1', '2026-01-01', 'Day', ?, 'finalized', 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """, (customer_id,))
    dispatch_id = conn.execute("SELECT id FROM dispatches WHERE dispatch_number='LEGACY-1'").fetchone()[0]
    conn.execute("""
        INSERT INTO dispatch_lines (dispatch_id, product_id, cartons, packs, pieces, base_unit_qty,
                                     packaging_rule_id, created_at, updated_at)
        VALUES (?, ?, 1, 0, 0, 100, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
    """, (dispatch_id, product_id, rule_id))
    conn.commit()
    conn.close()
    return customer_id, dispatch_id


def test_upgrade_preserves_pre_existing_customers_and_dispatches(bare_app):
    db_path = bare_app
    from webapp import create_app
    from flask_migrate import upgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade(revision=PRIOR_REVISION)

    customer_id, dispatch_id = _seed_pre_enhancement_data(db_path)

    with flask_app.app_context():
        upgrade(revision=HEAD_REVISION)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    assert customer["name"] == "Legacy Customer"
    assert customer["sales_category_id"] is None
    assert customer["is_temporary"] == 0
    assert customer["merged_into_id"] is None

    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    assert dispatch["dispatch_number"] == "LEGACY-1"
    assert dispatch["sales_category_id"] is None
    assert dispatch["customer_name_snapshot"] is None

    categories = conn.execute("SELECT name FROM sales_categories ORDER BY display_order").fetchall()
    assert [c["name"] for c in categories] == [
        "Corporate Sales", "Metro Sales", "Upcountry Sales", "Shop/Kikuubo Sales", "Factory Sales",
    ]
    conn.close()


def test_upgrade_is_reentrant_with_existing_data(bare_app):
    db_path = bare_app
    from webapp import create_app
    from flask_migrate import upgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade(revision=PRIOR_REVISION)
    _seed_pre_enhancement_data(db_path)
    with flask_app.app_context():
        upgrade(revision=HEAD_REVISION)
        upgrade(revision=HEAD_REVISION)  # must not error or duplicate seed rows

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sales_categories").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    conn.close()


def test_downgrade_removes_new_columns_but_keeps_customers_and_dispatches(bare_app):
    db_path = bare_app
    from webapp import create_app
    from flask_migrate import upgrade, downgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade(revision=PRIOR_REVISION)
    customer_id, dispatch_id = _seed_pre_enhancement_data(db_path)
    with flask_app.app_context():
        upgrade(revision=HEAD_REVISION)
        downgrade(revision=PRIOR_REVISION)

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sales_categories" not in tables
    assert "customers" in tables
    assert "dispatches" in tables

    customer_cols = {r[1] for r in conn.execute("PRAGMA table_info(customers)")}
    assert "sales_category_id" not in customer_cols
    assert "is_temporary" not in customer_cols
    assert "merged_into_id" not in customer_cols

    # the actual rows are untouched throughout
    row = conn.execute("SELECT name FROM customers WHERE id=?", (customer_id,)).fetchone()
    assert row[0] == "Legacy Customer"
    dispatch_row = conn.execute("SELECT dispatch_number FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    assert dispatch_row[0] == "LEGACY-1"
    conn.close()


def test_sales_category_migration_never_drops_entries_or_customers():
    """
    Static guard, specific to THIS migration: unlike migrations that create
    a table and legitimately drop it again in their own downgrade() (e.g.
    the dispatches migration dropping 'dispatches'), this migration only
    ADDS columns/tables — it must never contain a drop of 'entries' or
    'customers', both of which existed long before it.
    """
    import pathlib
    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "012e556ab7ad_add_sales_categories_and_recipient_.py"
    )
    text = script.read_text()
    for table in ("entries", "customers"):
        assert f"drop_table('{table}')" not in text
        assert f'drop_table("{table}")' not in text
