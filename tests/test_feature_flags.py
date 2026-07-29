"""
Stage 4: module-level feature flags. Existing-modules-enabled-by-default,
super_admin-only writes, backend route enforcement, page-level guards,
transitive dependency enforcement (block-or-cascade on disable, hard
rejection on enable), "disabling never deletes data", and audit logging.

Dependency graph under test: dispatch -> customer_management,
daily_figures -> dispatch, reporting -> [dispatch, daily_figures].
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Flag Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Flag Test Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Flag Test Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _flags(client):
    return {f["module_key"]: f["enabled"] for f in client.get("/api/feature-flags").get_json()}


# ---------- defaults ----------

def test_all_modules_enabled_by_default(client, login_as):
    login_as("root", "password123", "super_admin")
    flags = client.get("/api/feature-flags").get_json()
    modules = {f["module_key"] for f in flags}
    assert modules == {"dispatch", "daily_figures", "history_exports", "dashboard", "customer_management", "reporting"}
    assert all(f["enabled"] for f in flags)


# ---------- permissions ----------

def test_read_flags_requires_login(client):
    res = client.get("/api/feature-flags")
    assert res.status_code == 401


@pytest.mark.parametrize("role", ["super_admin", "manager", "operator", "viewer"])
def test_any_authenticated_role_can_read_flags(client, login_as, role):
    login_as(f"reader_{role}", "password123", role)
    res = client.get("/api/feature-flags")
    assert res.status_code == 200


@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_change_flags(client, login_as, role):
    login_as(f"writer_{role}", "password123", role)
    res = client.patch("/api/feature-flags/reporting", json={"enabled": False})
    assert res.status_code == 403


def test_super_admin_can_change_flags(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/reporting", json={"enabled": False})
    assert res.status_code == 200
    changed = res.get_json()["changed"]
    assert changed == [{"module_key": "reporting", "label": "Reporting", "enabled": False,
                         "updated_by": changed[0]["updated_by"], "updated_at": changed[0]["updated_at"]}]


def test_unknown_module_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/not_a_real_module", json={"enabled": False})
    assert res.status_code == 400


def test_enabled_field_required(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/reporting", json={})
    assert res.status_code == 400


def test_setting_same_value_is_a_noop(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/reporting", json={"enabled": True})
    assert res.status_code == 200
    assert res.get_json()["changed"] == []


# ---------- backend route enforcement ----------

def test_disabled_dashboard_blocks_dashboard_route(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dashboard", json={"enabled": False})
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 403


def test_disabled_dispatch_blocks_dispatch_routes_via_cascade(client, login_as, setup):
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    res = client.get("/api/dispatches")
    assert res.status_code == 403
    res = client.post("/api/dispatches", json={
        "dispatch_number": "FLAG-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_disabled_customer_management_blocks_customer_routes_via_cascade(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    res = client.get("/api/admin/customers")
    assert res.status_code == 403
    res = client.get("/api/admin/sales-categories")
    assert res.status_code == 403


def test_reenabling_restores_route_access(client, login_as, setup):
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    assert client.get("/api/dispatches").status_code == 403
    client.patch("/api/feature-flags/dispatch", json={"enabled": True})
    assert client.get("/api/dispatches").status_code == 200


# ---------- disabling never deletes data ----------

def test_disabling_dispatch_does_not_delete_existing_dispatches(client, login_as, app, setup):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "FLAG-PERSIST", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    with app.app_context():
        from webapp.models.dispatch import Dispatch
        assert Dispatch.query.filter_by(dispatch_number="FLAG-PERSIST").first() is not None

    client.patch("/api/feature-flags/dispatch", json={"enabled": True})
    res = client.get("/api/dispatches?dispatch_number=FLAG-PERSIST")
    assert res.status_code == 200
    assert any(r["id"] == d["id"] for r in res.get_json()["results"])


def test_cascade_disabling_customer_management_does_not_delete_dispatch_or_customer_data(client, login_as, app, setup):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "FLAG-CASCADE-PERSIST", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    with app.app_context():
        from webapp.models.dispatch import Dispatch
        from webapp.models.customer import Customer
        assert Dispatch.query.filter_by(dispatch_number="FLAG-CASCADE-PERSIST").first() is not None
        assert Customer.query.filter_by(name="Flag Test Customer").first() is not None


# ---------- dependency enforcement: disabling ----------

def test_disabling_customer_management_blocked_without_cascade_while_dispatch_enabled(client, login_as):
    """Customer Management cannot be disabled while Dispatch remains
    enabled unless Dispatch is atomically disabled too."""
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False})
    assert res.status_code == 400
    assert "cascade: true" in res.get_json()["error"]
    assert "Dispatch" in res.get_json()["error"]
    # nothing changed
    assert _flags(client)["customer_management"] is True
    assert _flags(client)["dispatch"] is True


def test_disabling_customer_management_cascades_to_dispatch_and_its_dependents(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    assert res.status_code == 200
    changed_keys = {f["module_key"] for f in res.get_json()["changed"]}
    assert changed_keys == {"customer_management", "dispatch", "daily_figures", "reporting"}
    assert all(f["enabled"] is False for f in res.get_json()["changed"])
    flags = _flags(client)
    assert flags["customer_management"] is False
    assert flags["dispatch"] is False
    assert flags["daily_figures"] is False
    assert flags["reporting"] is False
    # untouched by this cascade
    assert flags["dashboard"] is True
    assert flags["history_exports"] is True


def test_disabling_dispatch_blocked_without_cascade_while_daily_figures_and_reporting_enabled(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": False})
    assert res.status_code == 400
    error = res.get_json()["error"]
    assert "Daily Figures" in error and "Reporting" in error
    assert _flags(client)["dispatch"] is True


def test_disabling_dispatch_cascades_to_daily_figures_and_reporting(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    assert res.status_code == 200
    changed_keys = {f["module_key"] for f in res.get_json()["changed"]}
    assert changed_keys == {"dispatch", "daily_figures", "reporting"}
    flags = _flags(client)
    assert flags["dispatch"] is False
    assert flags["daily_figures"] is False
    assert flags["reporting"] is False
    assert flags["customer_management"] is True  # dispatch's own dependency is untouched


def test_disabling_dispatch_needs_no_cascade_once_its_dependents_are_already_off(client, login_as):
    login_as("root", "password123", "super_admin")
    # reporting depends on both dispatch and daily_figures; disable it
    # first so it's no longer an enabled dependent of either.
    res = client.patch("/api/feature-flags/reporting", json={"enabled": False})
    assert res.status_code == 200
    res = client.patch("/api/feature-flags/daily_figures", json={"enabled": False})
    assert res.status_code == 200  # reporting (its only dependent) is already off
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": False})
    assert res.status_code == 200  # daily_figures and reporting (its dependents) are both already off
    assert {f["module_key"] for f in res.get_json()["changed"]} == {"dispatch"}


def test_disabling_dashboard_never_needs_cascade(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/dashboard", json={"enabled": False})
    assert res.status_code == 200
    assert {f["module_key"] for f in res.get_json()["changed"]} == {"dashboard"}


def test_disabling_history_exports_never_needs_cascade(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/history_exports", json={"enabled": False})
    assert res.status_code == 200


# ---------- dependency enforcement: enabling ----------

def test_dispatch_cannot_be_enabled_while_customer_management_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": True})
    assert res.status_code == 400
    assert "Customer Management" in res.get_json()["error"]
    assert _flags(client)["dispatch"] is False  # unchanged


def test_daily_figures_cannot_be_enabled_while_dispatch_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    res = client.patch("/api/feature-flags/daily_figures", json={"enabled": True})
    assert res.status_code == 400
    assert "Dispatch" in res.get_json()["error"]


def test_reporting_cannot_be_enabled_unless_both_dispatch_and_daily_figures_enabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    res = client.patch("/api/feature-flags/reporting", json={"enabled": True})
    assert res.status_code == 400
    error = res.get_json()["error"]
    assert "Dispatch" in error and "Daily Figures" in error


def test_enabling_never_silently_enables_unrelated_modules(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    # attempting (and failing) to enable dispatch must not touch anything else
    client.patch("/api/feature-flags/dispatch", json={"enabled": True})
    flags = _flags(client)
    assert flags["customer_management"] is False
    assert flags["daily_figures"] is False
    assert flags["reporting"] is False
    assert flags["dashboard"] is True
    assert flags["history_exports"] is True


def test_full_reenable_chain_in_dependency_order_works(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    assert client.patch("/api/feature-flags/customer_management", json={"enabled": True}).status_code == 200
    assert client.patch("/api/feature-flags/dispatch", json={"enabled": True}).status_code == 200
    assert client.patch("/api/feature-flags/daily_figures", json={"enabled": True}).status_code == 200
    assert client.patch("/api/feature-flags/reporting", json={"enabled": True}).status_code == 200
    assert all(_flags(client).values())


# ---------- transactionality ----------

def test_cascade_change_is_all_or_nothing_in_one_response(client, login_as):
    """All modules affected by a cascade come back together in the same
    successful response — never a partial list."""
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    assert res.status_code == 200
    assert len(res.get_json()["changed"]) == 4


def test_blocked_dependency_change_leaves_every_flag_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    before = _flags(client)
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False})
    assert res.status_code == 400
    assert _flags(client) == before


def test_blocked_enable_leaves_every_flag_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    before = _flags(client)
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": True})
    assert res.status_code == 400
    assert _flags(client) == before


# ---------- audit logging ----------

def test_flag_change_is_audited_with_before_after(client, login_as, app):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/reporting", json={"enabled": False})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="update", entity_type="feature_flag", entity_id="reporting").first()
        assert entry is not None
        d = entry.to_dict()
        assert d["before"]["enabled"] is True
        assert d["after"]["enabled"] is False
        assert entry.username == "root"


def test_cascade_change_audits_every_affected_module(client, login_as, app):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        audited = {
            e.entity_id for e in AuditLog.query.filter_by(action="update", entity_type="feature_flag").all()
        }
        assert {"customer_management", "dispatch", "daily_figures", "reporting"} <= audited


def test_blocked_dependency_change_is_not_audited(client, login_as, app):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False})  # blocked, no cascade
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="update", entity_type="feature_flag", entity_id="customer_management").first()
        assert entry is None


# ---------- page-level guards ----------

def test_dispatch_page_redirects_when_dispatch_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    # Cascading dispatch off also takes daily_figures with it (a real
    # dependent) — so the fallback correctly skips straight past "/" to
    # the next still-enabled module page rather than landing on one that
    # would immediately bounce the user again.
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    res = client.get("/dispatch.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/history.html"


def test_history_page_redirects_when_history_exports_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/history_exports", json={"enabled": False})
    res = client.get("/history.html")
    assert res.status_code == 302


def test_dashboard_page_redirects_when_dashboard_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dashboard", json={"enabled": False})
    res = client.get("/dashboard.html")
    assert res.status_code == 302


def test_login_page_always_reachable_even_when_daily_figures_disabled(client, login_as):
    """Critical: "/" is the only login page — it must never be blocked by
    its own module's flag, or every login would break."""
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    res = client.get("/")
    assert res.status_code == 200


def test_login_page_reachable_unauthenticated_even_when_all_other_modules_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/history_exports", json={"enabled": False})
    client.post("/api/logout")
    res = client.get("/")
    assert res.status_code == 200


def test_all_three_operational_pages_disabled_returns_503_not_a_loop(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/dispatch", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/history_exports", json={"enabled": False})
    res = client.get("/dispatch.html")
    assert res.status_code == 503
    res = client.get("/history.html")
    assert res.status_code == 503
    assert client.get("/").status_code == 200


def test_admin_page_never_gated_by_any_flag(client, login_as):
    """A Super Administrator must always be able to reach admin.html to
    re-enable a module — it's the only way back."""
    login_as("root", "password123", "super_admin")
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/history_exports", json={"enabled": False})
    client.patch("/api/feature-flags/dashboard", json={"enabled": False})
    res = client.get("/admin.html")
    assert res.status_code == 200


# ---------- migration ----------

def test_feature_flags_migration_up_and_down(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "feature_flags_migration_test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)

    from webapp import create_app
    from flask_migrate import downgrade, upgrade

    flask_app = create_app()
    with flask_app.app_context():
        upgrade()

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT module_key, enabled FROM feature_flags ORDER BY module_key").fetchall()
    conn.close()
    assert rows == [
        ("customer_management", 1), ("daily_figures", 1), ("dashboard", 1),
        ("dispatch", 1), ("history_exports", 1), ("reporting", 1),
    ]

    with flask_app.app_context():
        downgrade(revision="8d16f14e2b4a")  # this migration's down_revision

    conn = sqlite3.connect(db_path)
    remaining = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "feature_flags" not in remaining
    assert "entries" in remaining
