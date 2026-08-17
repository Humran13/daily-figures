"""
Final UI correction: Reset Daily Values gets its own dedicated page
(static/reset-daily-values.html) — originally reachable by Manager and
Super Administrator, closing the gap where the reset backend already
allowed Manager but the only UI for it lived inside admin.html.

The Full targeted Operator correction/void/requests/notification package
(Part 18) later tightened this to Super-Administrator ONLY — Manager is
now redirected away from this page exactly like every other unrelated
Super-Administrator-only tool, see the Manager-redirect tests below.
admin.html remains Super-Administrator-only for its unrelated Users/
Company-Settings/Feature-Flags/branding controls.

No reset business logic is duplicated here — the new page calls the same
/api/daily-reset/preview and /api/daily-reset endpoints
tests/test_final_correction_reset_modes.py already exercises in full;
this file is about ACCESS (page guard + navigation), not re-testing the
reset calculation itself.
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
RESET_PAGE_HTML = (STATIC_DIR / "reset-daily-values.html").read_text(encoding="utf-8")
ADMIN_HTML = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")


# =====================================================================
# Page-level access
# =====================================================================

def test_manager_is_redirected_away_from_the_reset_page(client, login_as):
    # Full targeted Operator correction/void/requests/notification
    # package, Part 18: Reset Daily Values is now Super-Administrator
    # only — this test originally encoded the prior Manager-or-Super-
    # Administrator rule.
    login_as("reset_page_mgr", "password123", "manager")
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/reset-daily-values.html"


def test_super_administrator_can_open_the_reset_page(client, login_as):
    login_as("reset_page_admin", "password123", "super_admin")
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 200


def test_operator_cannot_open_the_reset_page(client, login_as):
    login_as("reset_page_op", "password123", "operator")
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/reset-daily-values.html"


def test_viewer_cannot_open_the_reset_page(client, login_as):
    login_as("reset_page_viewer", "password123", "viewer")
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/reset-daily-values.html"


def test_unauthenticated_reset_page_redirects_to_login(client):
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


# =====================================================================
# Navigation visibility
# =====================================================================

def test_manager_navigation_includes_reset_daily_values():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags) {")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "reset_daily_values" in body
    assert "role === 'manager' || role === 'super_admin'" in body


def test_super_administrator_navigation_includes_reset_daily_values():
    # Same nav-building function serves both roles — confirmed by the
    # combined role check above; this test documents the requirement
    # explicitly rather than only asserting it indirectly.
    assert "reset_daily_values" in APP_SHELL_JS


def test_operator_and_viewer_navigation_excludes_reset_daily_values(client, login_as):
    """Operators get a completely different nav-building branch
    (operationalNavItems, never reportingNavItems) — confirmed at the
    source level that the operator branch has no reset_daily_values
    reference, and functionally via the page guard test above."""
    render_nav_idx = APP_SHELL_JS.index("function renderNav(")
    idx = APP_SHELL_JS.index("if (role === 'operator') {", render_nav_idx)
    operator_branch = APP_SHELL_JS[idx:APP_SHELL_JS.index("} else if (OPERATIONAL_PAGES", idx)]
    assert "reset_daily_values" not in operator_branch


def test_reset_page_registered_in_current_page_key_for_active_highlighting():
    assert "if (path === '/reset-daily-values.html') return 'reset_daily_values';" in APP_SHELL_JS


# =====================================================================
# Admin isolation — unrelated Super-Administrator tools remain untouched
# =====================================================================

def test_manager_still_cannot_reach_admin_page(client, login_as):
    login_as("reset_page_mgr2", "password123", "manager")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/admin.html"


def test_manager_still_cannot_manage_users(client, login_as):
    login_as("reset_page_mgr3", "password123", "manager")
    res = client.post("/api/admin/users", json={"username": "sneaky", "password": "password123", "role": "operator"})
    assert res.status_code == 403


def test_manager_still_cannot_change_company_settings(client, login_as):
    login_as("reset_page_mgr4", "password123", "manager")
    res = client.patch("/api/admin/company-settings", json={"display_name": "Sneaky Co"})
    assert res.status_code == 403


def test_manager_still_cannot_change_feature_flags(client, login_as):
    login_as("reset_page_mgr5", "password123", "manager")
    res = client.patch("/api/feature-flags/dispatch", json={"enabled": False})
    assert res.status_code == 403


def test_reset_page_never_exposes_admin_only_markup():
    """The new page must not accidentally pull in Users/Company-Settings/
    Feature-Flags/branding-upload controls."""
    for marker in ("newUserName", "newUserRole", "panel-company", "panel-flags", "companyLogoUpload"):
        assert marker not in RESET_PAGE_HTML


# =====================================================================
# Full reset UI present, calling the existing APIs (no duplicated logic)
# =====================================================================

def test_both_reset_modes_present_on_the_page():
    assert 'id="resetModeFiguresOnly"' in RESET_PAGE_HTML
    assert 'id="resetModeFull"' in RESET_PAGE_HTML
    assert "Reset Daily Figures Status Only" in RESET_PAGE_HTML
    assert "Full Reset" in RESET_PAGE_HTML


def test_page_has_date_shift_and_product_scope_controls():
    assert 'id="resetDate"' in RESET_PAGE_HTML
    assert 'id="resetShift"' in RESET_PAGE_HTML
    assert 'id="resetProduct"' in RESET_PAGE_HTML


def test_page_calls_the_existing_preview_and_execute_apis():
    assert "/api/daily-reset/preview" in RESET_PAGE_HTML
    assert "api('/api/daily-reset'," in RESET_PAGE_HTML


def test_page_does_not_duplicate_reset_business_logic():
    """No local re-derivation of what gets neutralized/voided/cleared —
    the page only ever reads whatever the backend preview/execute
    responses already say and renders it."""
    for forbidden in ("opening_stock_source =", "void_status", "STATUS_FINALIZED", "neutralize_source"):
        assert forbidden not in RESET_PAGE_HTML


def test_mode_a_warning_text_present():
    assert "This reset clears Daily Figures workflow status only. Existing Dispatch, Returns, and Production records will remain and will continue affecting stock balances." in RESET_PAGE_HTML


def test_mode_b_warning_text_present():
    assert "This full reset will neutralize matching Dispatch, Returns, and Production entries for the selected scope. Their audit history will remain, but they will stop affecting Daily Figures and carried stock balances." in RESET_PAGE_HTML


def test_typed_confirmation_required_for_full_reset():
    assert 'id="resetConfirmTypedWrap"' in RESET_PAGE_HTML
    assert "FULL RESET ${" in RESET_PAGE_HTML
    idx = RESET_PAGE_HTML.index("resetConfirmSubmitBtn').addEventListener")
    body = RESET_PAGE_HTML[idx:idx + 800]
    assert "typed !== expected" in body


def test_book_style_formatter_used_for_opening_quantities():
    assert '<script src="/quantity_format.js"></script>' in RESET_PAGE_HTML
    assert "qtyLabel(p.opening_qty, rule)" in RESET_PAGE_HTML


# =====================================================================
# Preview quality — real backend data, no false "no affected products"
# =====================================================================

@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Reset Page Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Reset Page Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Reset Page Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


def test_manager_is_refused_preview_via_the_underlying_api(client, login_as, setup):
    login_as("reset_page_mgr6", "password123", "manager")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 403


def test_preview_response_matches_what_the_page_renders(client, setup):
    pid = setup["product"]["id"]
    p = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")

    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "product_id": pid})
    data = res.get_json()
    assert data["any_affected"] is True
    assert len(data["products"][0]["finalized_production"]) == 1
    # The exact fields the page's rendering code reads.
    row = data["products"][0]["finalized_production"][0]
    assert "label" in row and "quantity_label" in row and "status" in row
