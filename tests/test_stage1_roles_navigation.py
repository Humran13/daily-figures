"""
Stage 1: role-based navigation, server-side page/API authorization, and the
role-wide Operator Daily-Figures permission flags. Covers exactly what the
Stage 1 spec required — nav/permission scaffolding — not the later stages
(History & Exports consolidation, branding, feature flags).
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    """super_admin session used to seed one product+rule+category+customer."""
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Stage1 Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Stage1 Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Stage1 Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


# ---------- Dashboard / Reports API access ----------

def test_operator_cannot_get_dashboard_api(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 403


def test_viewer_cannot_get_dashboard_api(client, login_as):
    login_as("view1", "password123", "viewer")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 403


def test_manager_can_get_dashboard_api(client, login_as):
    login_as("mgr1", "password123", "manager")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 200


def test_super_admin_can_get_dashboard_api(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 200


def test_operator_cannot_get_reports_summary(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/api/reports/summary?date_from=2026-07-01&date_to=2026-07-28")
    assert res.status_code == 403


def test_manager_can_get_reports_summary(client, login_as):
    login_as("mgr1", "password123", "manager")
    res = client.get("/api/reports/summary?date_from=2026-07-01&date_to=2026-07-28")
    assert res.status_code == 200


# ---------- Page-level guards (direct URL access) ----------

def test_operator_redirected_away_from_dashboard_page(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_viewer_redirected_away_from_dashboard_page(client, login_as):
    login_as("view1", "password123", "viewer")
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_operator_redirected_away_from_admin_page(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_manager_redirected_away_from_admin_page(client, login_as):
    # admin.html has always been super_admin-only; Stage 1 must not expand
    # Manager's scope, so Manager still cannot reach it.
    login_as("mgr1", "password123", "manager")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dashboard.html"


def test_manager_can_reach_dashboard_page(client, login_as):
    login_as("mgr1", "password123", "manager")
    res = client.get("/dashboard.html")
    assert res.status_code == 200


def test_super_admin_can_reach_admin_page(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.get("/admin.html")
    assert res.status_code == 200


def test_unauthenticated_dashboard_page_redirects_to_login(client):
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


def test_unauthenticated_admin_page_redirects_to_login(client):
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


def test_operator_can_still_reach_dispatch_and_daily_figures_pages(client, login_as):
    # only Dashboard/Admin are guarded — Operator's three operational pages
    # must load normally.
    login_as("op1", "password123", "operator")
    assert client.get("/dispatch.html").status_code == 200
    assert client.get("/").status_code == 200


# ---------- Viewer remains completely read-only ----------

def test_viewer_cannot_create_dispatch(client, login_as, setup):
    login_as("view1", "password123", "viewer")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "VIEWER-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_viewer_cannot_upsert_daily_figures(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("view1", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_viewer_cannot_create_adjustment(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("view1", "password123", "viewer")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "test",
    })
    assert res.status_code == 403


def test_viewer_still_cannot_create_adjustment_even_if_all_operator_flags_true(client, login_as, setup):
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={
        "can_edit_opening": True, "can_edit_production": True,
        "can_edit_returns": True, "can_create_adjustments": True,
    })
    login_as("view1", "password123", "viewer")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "should still fail",
    })
    assert res.status_code == 403


# ---------- Operator Daily-Figures permission flags ----------

def test_operator_daily_figure_permissions_default_all_false(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.get("/api/admin/operator-daily-figure-permissions")
    assert res.status_code == 200
    data = res.get_json()
    assert data["can_edit_opening"] is False
    assert data["can_edit_production"] is False
    assert data["can_edit_returns"] is False
    assert data["can_create_adjustments"] is False


@pytest.mark.parametrize("role", ["super_admin", "manager", "operator", "viewer"])
def test_any_authenticated_role_can_read_permissions(client, login_as, role):
    login_as(f"reader_{role}", "password123", role)
    res = client.get("/api/admin/operator-daily-figure-permissions")
    assert res.status_code == 200


def test_unauthenticated_cannot_read_permissions(client):
    res = client.get("/api/admin/operator-daily-figure-permissions")
    assert res.status_code == 401


@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_change_permissions(client, login_as, role):
    login_as(f"changer_{role}", "password123", role)
    res = client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_returns": True})
    assert res.status_code == 403


def test_super_admin_can_change_permissions(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_returns": True})
    assert res.status_code == 200
    assert res.get_json()["can_edit_returns"] is True


def test_permission_changes_are_audited(client, login_as, app):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_returns": True})
    assert res.status_code == 200

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="update", entity_type="operator_daily_figure_permissions").first()
        assert entry is not None
        assert entry.username == "root"
        d = entry.to_dict()
        assert d["before"]["can_edit_returns"] is False
        assert d["after"]["can_edit_returns"] is True


# Stage 5 moved Return/Production entry to the dedicated Returns Book /
# Production Book — Daily Figures only ever displays them now, for every
# role, exactly like Issued already was. The can_edit_returns/
# can_edit_production flags still exist on the OperatorDailyFigurePermissions
# model (their admin API below is unchanged), but no longer gate anything
# in the Daily Figures upsert route: there is nothing left there for them
# to permit. See tests/test_returns.py and tests/test_production.py for the
# role/permission checks that now apply to creating/finalizing/voiding a
# Returns/Production record instead.

def test_operator_cannot_influence_return_via_daily_figures_payload(client, login_as, setup):
    pid = setup["product"]["id"]
    # setup already logged in as super_admin "root" — reuse that session.
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })

    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Night",
        "return_": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["return_"]["base_qty"] == 0


def test_operator_cannot_influence_production_via_daily_figures_payload(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })

    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Night",
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["production"]["base_qty"] == 0


def test_operator_permission_flags_no_longer_gate_return_or_production_in_daily_figures(client, login_as, setup):
    """Even with both flags explicitly enabled, return_/production posted
    through Daily Figures still has zero effect — proving the removal is
    complete and not merely permission-gated."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    assert client.patch("/api/admin/operator-daily-figure-permissions",
                         json={"can_edit_returns": True, "can_edit_production": True}).status_code == 200

    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Night",
        "return_": {"cartons": 2, "packs": 0, "pieces": 0},
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["return_"]["base_qty"] == 0
    assert body["production"]["base_qty"] == 0


def test_operator_can_create_adjustment_when_permitted(client, login_as, setup):
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_create_adjustments": True})

    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 3, "reason": "operator adjustment",
    })
    assert res.status_code == 201


def test_manager_daily_figures_editing_unaffected_by_operator_flags(client, login_as, setup):
    """Manager's existing unconditional access must not change in Stage 1."""
    pid = setup["product"]["id"]
    login_as("mgr1", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        "return_": {"cartons": 1, "packs": 0, "pieces": 0},
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200


def test_manager_can_create_adjustment_unconditionally(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("mgr1", "password123", "manager")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "test",
    })
    assert res.status_code == 201


def test_operator_cannot_influence_issued_via_daily_figures_payload(client, login_as, setup):
    """Issued has no writable column anywhere — an extraneous 'issued' key
    in the payload must be silently ignored, regardless of permissions."""
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={
        "can_edit_opening": True, "can_edit_production": True, "can_edit_returns": True,
    })

    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
        "issued": {"cartons": 999, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["issued"]["base_qty"] == 0


# ---------- Migration ----------

def test_operator_permissions_migration_up_and_down(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "op_perm_migration_test.db"
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
    row = conn.execute(
        "SELECT can_edit_opening, can_edit_production, can_edit_returns, can_create_adjustments "
        "FROM operator_daily_figure_permissions WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row == (0, 0, 0, 0)

    with flask_app.app_context():
        downgrade(revision="135b08c5ab45")  # this migration's down_revision

    conn = sqlite3.connect(db_path)
    remaining = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "operator_daily_figure_permissions" not in remaining
    assert "entries" in remaining  # legacy table always survives
