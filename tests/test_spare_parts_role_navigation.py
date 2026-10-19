"""
Regression guards for the Spare Parts Planning/Master Data/Admin
Super-Administrator-only restriction, and the healthy-stock quantity
color fix on the Spare Parts dashboard.

Planning and Master Data were previously reachable (Planning: every
role; Master Data: Manager+Super Admin). Both are now Super-Admin-only
at the nav AND page level. Planning's backend (GET /api/spare-parts/planning
and its exports) is ALSO Super-Admin-only end to end — see
webapp/routes/spare_part_planning.py's module docstring for why
(the report mixes operational and procurement/value-derived data that is
unsafe to sanitize field-by-field). Master Data's write APIs (spare
part/category/machine/department/supplier CRUD, stock adjustment/count/
opening-balance) keep their existing Manager+Super Admin checks, since
those are genuine operational actions other tests already exercise
independently of this page, and financial fields on those endpoints were
already Super-Admin-only via _may_see_pricing()/include_pricing.
"""
from pathlib import Path

from tests.test_spare_parts import _create_spare_part, _login_spare_parts

# Any JSON key anywhere in a non-Super-Admin Spare Parts response that
# would leak or let a client reconstruct money/pricing/procurement-value
# information.
FORBIDDEN_FINANCIAL_KEYS = {
    "buying_price", "china_buying_price", "local_buying_price", "selling_price",
    "current_cost_price", "preferred_cost_source", "profit", "margin_percent",
    "china_projected_cost", "local_projected_cost", "selected_source_projected_cost",
    "estimated_savings", "procurement_value_12m",
}


def _assert_no_financial_keys(value, path=""):
    if isinstance(value, dict):
        for key, sub in value.items():
            assert key not in FORBIDDEN_FINANCIAL_KEYS, f"leaked financial key '{key}' at {path}"
            _assert_no_financial_keys(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_no_financial_keys(item, f"{path}[{i}]")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SPARE_PARTS_SHELL_JS = (STATIC_DIR / "spare-parts-shell.js").read_text(encoding="utf-8")
SPARE_PARTS_HTML = (STATIC_DIR / "spare-parts.html").read_text(encoding="utf-8")


# ---------- source-level nav regression guards ----------

def test_spare_parts_nav_restricts_planning_master_admin_to_super_admin():
    assert (
        "{ key: 'planning', label: 'Planning', href: '/spare-parts-planning.html', "
        "roles: ['super_admin'] }"
    ) in SPARE_PARTS_SHELL_JS
    assert (
        "{ key: 'master', label: 'Master Data', href: '/spare-parts-master.html', "
        "roles: ['super_admin'] }"
    ) in SPARE_PARTS_SHELL_JS
    assert (
        "{ key: 'admin', label: 'Admin', href: '/admin.html', roles: ['super_admin'] }"
    ) in SPARE_PARTS_SHELL_JS


def test_spare_parts_nav_leaves_dashboard_stock_in_out_history_unrestricted():
    assert "{ key: 'dashboard', label: 'Dashboard', href: '/spare-parts.html', roles: null }" in SPARE_PARTS_SHELL_JS
    assert "{ key: 'history', label: 'History', href: '/spare-parts-history.html', roles: null }" in SPARE_PARTS_SHELL_JS
    # Stock In/Stock Out: Operator/Manager/Accountant/Super Admin (see
    # test_accountant_spare_parts_stock_movements.py for the dedicated
    # Accountant coverage of this).
    assert "roles: ['operator', 'manager', 'accountant', 'super_admin']" in SPARE_PARTS_SHELL_JS


# ---------- page-level authorization matrix ----------

def test_super_admin_reaches_planning_and_master_data_pages(client, make_user):
    _login_spare_parts(client, make_user, "navsa1", "super_admin")
    assert client.get("/spare-parts-planning.html").status_code == 200
    assert client.get("/spare-parts-master.html").status_code == 200


def test_manager_denied_direct_access_to_planning_and_master_data_pages(client, make_user):
    _login_spare_parts(client, make_user, "navmgr1", "manager")
    for page in ("/spare-parts-planning.html", "/spare-parts-master.html"):
        res = client.get(page)
        assert res.status_code == 302, page
        assert res.headers["Location"] == "/spare-parts.html", page


def test_accountant_denied_direct_access_to_planning_and_master_data_pages(client, make_user):
    _login_spare_parts(client, make_user, "navacc1", "accountant")
    for page in ("/spare-parts-planning.html", "/spare-parts-master.html"):
        res = client.get(page)
        assert res.status_code == 302, page
        assert res.headers["Location"] == "/spare-parts.html", page


def test_operator_denied_direct_access_to_planning_and_master_data_pages(client, make_user):
    _login_spare_parts(client, make_user, "navop1", "operator")
    for page in ("/spare-parts-planning.html", "/spare-parts-master.html"):
        res = client.get(page)
        assert res.status_code == 302, page
        assert res.headers["Location"] == "/spare-parts.html", page


def test_viewer_denied_direct_access_to_planning_and_master_data_pages(client, make_user):
    _login_spare_parts(client, make_user, "navvw1", "viewer")
    for page in ("/spare-parts-planning.html", "/spare-parts-master.html"):
        res = client.get(page)
        assert res.status_code == 302, page
        assert res.headers["Location"] == "/spare-parts.html", page


def test_non_super_admin_still_reaches_dashboard_stock_in_out_history(client, make_user):
    # Unchanged legitimate access — only Planning/Master Data/Admin moved.
    _login_spare_parts(client, make_user, "navmgr2", "manager")
    assert client.get("/spare-parts.html").status_code == 200
    assert client.get("/spare-parts-stock-in.html").status_code == 200
    assert client.get("/spare-parts-stock-out.html").status_code == 200
    assert client.get("/spare-parts-history.html").status_code == 200


# ---------- privilege containment: Admin stays out of reach too ----------

def test_accountant_still_cannot_reach_spare_parts_admin_page(client, make_user):
    _login_spare_parts(client, make_user, "navacc2", "accountant")
    res = client.get("/admin.html")
    assert res.status_code == 302


# ---------- healthy-stock quantity color (Task 1) ----------

def test_healthy_stock_quantity_uses_green_color_not_whole_row():
    assert ".inventory-row.healthy .stock{ color:var(--teal); }" in SPARE_PARTS_HTML
    # Low/no-stock must keep their existing red presentation, unregressed.
    assert ".inventory-row.low .stock, .inventory-row.no_stock .stock{ color:var(--red); }" in SPARE_PARTS_HTML
    # The healthy rule must not recolor the whole row (name/code/minimum) —
    # only .stock is targeted, never a bare ".inventory-row.healthy{...}".
    assert ".inventory-row.healthy{" not in SPARE_PARTS_HTML


# ---------- Planning backend: Super-Admin-only end to end ----------

def test_non_super_admin_denied_planning_api(client, make_user):
    for role, username in (("manager", "navplan_mgr"), ("accountant", "navplan_acct"),
                            ("operator", "navplan_op"), ("viewer", "navplan_view")):
        other = client.application.test_client()
        _login_spare_parts(other, make_user, username, role)
        res = other.get("/api/spare-parts/planning")
        assert res.status_code == 403, role
        res = other.get("/api/spare-parts/planning/export.csv")
        assert res.status_code == 403, role


def test_super_admin_allowed_planning_api_and_export(client, make_user):
    _login_spare_parts(client, make_user, "navplan_sa", "super_admin")
    assert client.get("/api/spare-parts/planning").status_code == 200
    assert client.get("/api/spare-parts/planning/export.csv").status_code == 200


# ---------- financial-leakage audit: ordinary Spare Parts responses ----------

def test_non_super_admin_spare_parts_responses_contain_no_financial_keys(client, make_user):
    _login_spare_parts(client, make_user, "leaksa_setup", "super_admin")
    part = _create_spare_part(client, name="Leak Audit Part")
    client.patch(f"/api/spare-parts/{part['id']}", json={
        "china_buying_price": "10.00", "local_buying_price": "12.00", "selling_price": "20.00",
    })

    for role, username in (("manager", "leak_mgr"), ("accountant", "leak_acct"),
                            ("operator", "leak_op"), ("viewer", "leak_view")):
        other = client.application.test_client()
        _login_spare_parts(other, make_user, username, role)
        list_res = other.get("/api/spare-parts")
        assert list_res.status_code == 200, role
        _assert_no_financial_keys(list_res.get_json(), f"{role}:/api/spare-parts")

        detail_res = other.get(f"/api/spare-parts/{part['id']}")
        assert detail_res.status_code == 200, role
        _assert_no_financial_keys(detail_res.get_json(), f"{role}:/api/spare-parts/{{id}}")

        dashboard_res = other.get("/api/spare-parts/dashboard")
        assert dashboard_res.status_code == 200, role
        _assert_no_financial_keys(dashboard_res.get_json(), f"{role}:/api/spare-parts/dashboard")

        history_res = other.get("/api/spare-parts/movements")
        assert history_res.status_code == 200, role
        _assert_no_financial_keys(history_res.get_json(), f"{role}:/api/spare-parts/movements")


def test_admin_pricing_api_remains_super_admin_only(client, make_user):
    for role, username in (("manager", "adminpx_mgr"), ("accountant", "adminpx_acct"),
                            ("operator", "adminpx_op"), ("viewer", "adminpx_view")):
        other = client.application.test_client()
        _login_spare_parts(other, make_user, username, role)
        assert other.get("/api/admin/spare-parts/pricing").status_code == 403, role


# ---------- Manager's legitimate non-financial operational actions survive ----------

def test_manager_can_still_create_edit_and_correct_stock_outside_master_data_page(client, make_user):
    _login_spare_parts(client, make_user, "opsmgr1", "manager")
    part = _create_spare_part(client, name="Operational Survival Part")
    assert client.patch(f"/api/spare-parts/{part['id']}", json={"model": "Rev B"}).status_code == 200

    from tests.test_spare_parts import _get_or_create_department
    dept_id = _get_or_create_department(client.application, "Leak Audit Dept")
    adj = client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "5", "direction": "in", "reason": "initial stock",
    })
    assert adj.status_code == 201
    count = client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "5", "reason": "physical count",
    })
    assert count.status_code == 201
    assert client.post("/api/spare-parts/categories", json={"name": "Opsurvival Category"}).status_code == 201
