"""
Regression guards for the Spare Parts Planning/Master Data/Admin
Super-Administrator-only restriction, and the healthy-stock quantity
color fix on the Spare Parts dashboard.

Planning and Master Data were previously reachable (Planning: every
role; Master Data: Manager+Super Admin). Both are now Super-Admin-only
at the nav and page level. The underlying APIs are deliberately left as
they were (see webapp/routes/pages.py's docstrings on those two routes):
Planning's read API still serves every role (financial fields already
withheld from non-Super-Admin via _may_see_pricing()), and Master Data's
write APIs (spare part/category/machine/department/supplier CRUD, stock
adjustment/count/opening-balance) keep their existing Manager+Super Admin
checks, since those are genuine operational actions other tests already
exercise independently of this page.
"""
from pathlib import Path

from tests.test_spare_parts import _login_spare_parts

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
    assert "roles: ['operator', 'manager', 'super_admin']" in SPARE_PARTS_SHELL_JS


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
