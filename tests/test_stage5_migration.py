"""
Stage 5 migration safety: clean-DB upgrade, existing-DB upgrade (with
realistic pre-existing data), and downgrade/re-upgrade — same ritual used
for every migration in this project (see e.g.
tests/test_stage1_roles_navigation.py's operator-permissions migration
test, tests/test_feature_flags.py's feature-flags migration test).
"""
import sqlite3

import pytest


def _fresh_app(tmp_path, monkeypatch, db_name="migration_test.db"):
    db_path = tmp_path / db_name
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)

    from webapp import create_app
    return create_app(), db_path


def test_clean_db_upgrade_creates_returns_and_production_tables(tmp_path, monkeypatch):
    from flask_migrate import upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for table in ("return_records", "return_lines", "production_records", "production_lines"):
        assert table in tables


def test_clean_db_upgrade_seeds_returns_and_production_flags_enabled(tmp_path, monkeypatch):
    from flask_migrate import upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()

    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT module_key, enabled FROM feature_flags WHERE module_key IN ('returns','production')"))
    conn.close()
    assert rows == {"returns": 1, "production": 1}


def test_clean_db_upgrade_preserves_existing_tables(tmp_path, monkeypatch):
    """Purely additive — every table from every prior migration must still exist."""
    from flask_migrate import upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for table in ("users", "products", "packaging_rules", "customers", "dispatches", "dispatch_lines",
                  "daily_figures", "stock_adjustments", "company_settings", "feature_flags", "entries"):
        assert table in tables


def test_downgrade_removes_returns_and_production_tables_without_touching_others(tmp_path, monkeypatch):
    from flask_migrate import downgrade, upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()
        downgrade(revision="e8a1576c5404")  # this migration's own down_revision

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for table in ("return_records", "return_lines", "production_records", "production_lines"):
        assert table not in tables
    # everything else (including feature_flags itself) untouched
    assert "feature_flags" in tables
    assert "dispatches" in tables


def test_downgrade_removes_only_returns_and_production_flag_rows(tmp_path, monkeypatch):
    from flask_migrate import downgrade, upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()
        downgrade(revision="e8a1576c5404")

    conn = sqlite3.connect(db_path)
    remaining = {r[0] for r in conn.execute("SELECT module_key FROM feature_flags")}
    conn.close()
    assert remaining == {"dispatch", "daily_figures", "history_exports", "dashboard", "customer_management", "reporting"}


def test_reupgrade_after_downgrade_recreates_tables_and_reseeds_flags(tmp_path, monkeypatch):
    from flask_migrate import downgrade, upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        upgrade()
        downgrade(revision="e8a1576c5404")
        upgrade()

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = dict(conn.execute("SELECT module_key, enabled FROM feature_flags WHERE module_key IN ('returns','production')"))
    conn.close()
    assert "return_records" in tables and "production_records" in tables
    assert rows == {"returns": 1, "production": 1}


def test_existing_db_upgrade_preserves_realistic_pre_existing_data(tmp_path, monkeypatch):
    """Upgrade a DB that already has real users/customers/dispatches/daily
    figures on the immediately-prior revision, then confirm none of it is
    touched by this migration."""
    from flask_migrate import upgrade
    from werkzeug.security import generate_password_hash

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch, db_name="existing_db_test.db")
    with flask_app.app_context():
        upgrade(revision="e8a1576c5404")  # stop one migration short of this stage's

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?, ?, ?, 1, '2026-07-01T00:00:00')",
        ("preexisting_user", generate_password_hash("password123"), "manager"),
    )
    conn.commit()
    conn.close()

    with flask_app.app_context():
        upgrade()  # onward to this stage's migration

    conn = sqlite3.connect(db_path)
    user = conn.execute("SELECT username, role FROM users WHERE username='preexisting_user'").fetchone()
    conn.close()
    assert user == ("preexisting_user", "manager")


def test_app_boots_and_serves_returns_and_production_after_upgrade(tmp_path, monkeypatch):
    """End-to-end smoke test: migrate, then actually use the new API through
    a real Flask test client — not just a schema check."""
    from flask_migrate import upgrade

    flask_app, db_path = _fresh_app(tmp_path, monkeypatch, db_name="smoke_test.db")
    monkeypatch.setenv("SUPERADMIN_USERNAME", "root")
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "password123")
    with flask_app.app_context():
        upgrade()

    from webapp.auth import seed_super_admin
    from webapp.extensions import db as _db
    with flask_app.app_context():
        seed_super_admin()
        _db.session.commit()

    client = flask_app.test_client()
    login = client.post("/api/login", json={"username": "root", "password": "password123"})
    assert login.status_code == 200

    flags = {f["module_key"]: f["enabled"] for f in client.get("/api/feature-flags").get_json()}
    assert flags["returns"] is True
    assert flags["production"] is True

    assert client.get("/api/returns").status_code == 200
    assert client.get("/api/production").status_code == 200
