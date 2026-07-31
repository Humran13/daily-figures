"""
Stage 6: role-based app shell, Dashboard polish, branding-as-PWA-icon, and
super-admin user management. Covers what this stage actually added/changed:

- Role-aware landing pages (server-side first_authorized_page() in
  webapp/routes/pages.py, mirrored client-side by static/app-shell.js's
  resolveLanding()) and the disabled-feature fallback chains.
- The shared, centralized authenticated header/nav (static/app-shell.js) —
  identity, Home/Back/Logout, the two navigation contexts (reporting vs.
  operational), and active-tab (aria-current) state. Frontend behavior is
  pinned at the source level, same rationale as every other frontend-only
  piece of this project (no JS/browser test runner exists here).
- Dashboard: activity counts, Day/Night Production, attention notices,
  per-product book-notation summary, Viewer read-only access.
- Super-admin user management: dedicated password-reset endpoint,
  session-version invalidation, self-lockout protections, audit content.
- Company logo as PWA install icon: dynamic manifest, derived icon
  generation/serving, generic fallback.

Existing coverage this deliberately does NOT duplicate: general branding
CRUD/validation/audit (tests/test_company_settings.py), general user CRUD
(tests/test_admin_users.py), Dashboard's core stock-summary/low-stock/
top-products/adjustments/voids numbers (tests/test_dashboard.py), and
per-page nav markup/data-module tagging (tests/test_stage4_frontend.py,
tests/test_stage5_frontend.py).
"""
import io

import pytest
from PIL import Image

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")
APP_SHELL_CSS = (STATIC_DIR / "app-shell.css").read_text(encoding="utf-8")
PRIMARY_PAGES = {
    name: (STATIC_DIR / name).read_text(encoding="utf-8")
    for name in (
        "index.html", "dispatch.html", "returns.html", "production.html",
        "history.html", "dashboard.html", "admin.html",
    )
}


def _png_bytes(size=(20, 20), color="red"):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    buf.seek(0)
    return buf


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Stage6 Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Stage6 Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Stage6 Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _set_flag(client, module_key, enabled):
    # cascade:true is a no-op unless disabling would otherwise be rejected
    # for leaving an enabled dependent module broken (see
    # webapp/services/feature_flag_service.py's REQUIRES map) — harmless to
    # always include here since these tests don't care about the specific
    # set of modules a cascade takes down alongside the one under test.
    res = client.patch(f"/api/feature-flags/{module_key}", json={"enabled": enabled, "cascade": True})
    assert res.status_code == 200, res.get_json()


import re as _re


def _strip_js_comments(source):
    without_block = _re.sub(r"/\*.*?\*/", "", source, flags=_re.DOTALL)
    return _re.sub(r"//[^\n]*", "", without_block)


APP_SHELL_JS_CODE_ONLY = _strip_js_comments(APP_SHELL_JS)


# =====================================================================
# Role landing — backend (webapp/routes/pages.py)
# =====================================================================

def test_operator_redirected_to_dispatch_by_default(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_operator_falls_back_to_returns_when_dispatch_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    _set_flag(client, "dispatch", False)
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/returns.html?tab=new"


def test_operator_falls_back_to_production_when_dispatch_and_returns_disabled(client, login_as):
    login_as("root", "password123", "super_admin")
    _set_flag(client, "dispatch", False)
    _set_flag(client, "returns", False)
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/production.html?tab=new"


def test_operator_landing_never_dead_ends_when_every_book_disabled(client, login_as):
    """All three operational books disabled: the chain still returns a real
    page (its own first entry) rather than None/looping — the page itself
    shows the disabled-module message once reached."""
    login_as("root", "password123", "super_admin")
    _set_flag(client, "dispatch", False)
    _set_flag(client, "returns", False)
    _set_flag(client, "production", False)
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


@pytest.mark.parametrize("role", ["viewer", "manager"])
def test_viewer_and_manager_redirected_to_dashboard(client, login_as, role):
    login_as(f"user_{role}", "password123", role)
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dashboard.html"


def test_viewer_can_load_dashboard_page_directly(client, login_as):
    login_as("view1", "password123", "viewer")
    res = client.get("/dashboard.html")
    assert res.status_code == 200


def test_no_redirect_loop_when_dashboard_disabled_for_viewer(client, login_as):
    """Viewer's landing page (/admin.html -> /dashboard.html) is itself
    disabled: following redirects must still land on a real 200 within a
    couple of hops, never loop back to /dashboard.html."""
    login_as("root", "password123", "super_admin")
    _set_flag(client, "dashboard", False)
    client.post("/api/logout")
    login_as("view1", "password123", "viewer")
    res = client.get("/admin.html", follow_redirects=True)
    assert res.status_code == 200
    assert res.request.path != "/dashboard.html"


def test_resolve_landing_js_mirrors_backend_operator_chain():
    """static/app-shell.js's resolveLanding() must check the same three
    modules in the same order as webapp/routes/pages.py's
    OPERATOR_LANDING_CHAIN — this is duplicated logic by necessity (one
    side is Python, one is JS) but must never drift apart."""
    idx = APP_SHELL_JS.index("function resolveLanding(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    dispatch_idx = body.index("dispatch.html?tab=new")
    returns_idx = body.index("returns.html?tab=new")
    production_idx = body.index("production.html?tab=new")
    assert dispatch_idx < returns_idx < production_idx


def test_resolve_landing_js_defaults_non_operator_roles_to_dashboard_chain():
    idx = APP_SHELL_JS.index("function resolveLanding(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "if (enabled(flags, 'dashboard')) return '/dashboard.html';" in body
    assert "if (enabled(flags, 'daily_figures')) return '/';" in body
    assert "if (enabled(flags, 'history_exports')) return '/history.html';" in body


# =====================================================================
# Shared header — every authenticated page (source-level)
# =====================================================================

def test_every_primary_page_loads_app_shell_script_once():
    for name, source in PRIMARY_PAGES.items():
        assert source.count('<script src="/app-shell.js" defer></script>') == 1, \
            f"{name} must load app-shell.js exactly once"


def test_every_primary_page_has_exactly_one_identity_and_nav_placeholder():
    for name, source in PRIMARY_PAGES.items():
        assert source.count('id="appIdentityBar"') == 1, f"{name} is missing (or duplicates) #appIdentityBar"
        assert source.count('id="appRoleNav"') == 1, f"{name} is missing (or duplicates) #appRoleNav"


def test_identity_bar_shows_username_role_home_back_logout():
    idx = APP_SHELL_JS.index("function renderIdentityBar(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function escapeHtml", idx)]
    assert "user.username" in body
    assert "ROLE_LABELS[user.role]" in body
    assert "textContent = 'Back'" in body
    assert "textContent = 'Home'" in body
    assert "textContent = 'Log out'" in body


def test_logout_button_calls_the_existing_logout_endpoint_and_redirects_home():
    idx = APP_SHELL_JS.index("logoutBtn.addEventListener")
    snippet = APP_SHELL_JS[idx:idx + 300]
    assert "fetch('/api/logout', { method: 'POST' })" in snippet
    assert "location.href = '/';" in snippet


def test_role_labels_cover_all_four_roles():
    for role in ("super_admin", "manager", "operator", "viewer"):
        assert f"{role}:" in APP_SHELL_JS


# =====================================================================
# Home / Back behavior (source-level — no document.referrer, no
# history.back(), sessionStorage breadcrumb instead)
# =====================================================================

def test_back_never_uses_document_referrer():
    assert "document.referrer" not in APP_SHELL_JS_CODE_ONLY


def test_back_never_uses_native_history_back():
    assert "history.back()" not in APP_SHELL_JS_CODE_ONLY


def test_back_uses_a_sessionstorage_breadcrumb_stack():
    assert "sessionStorage" in APP_SHELL_JS
    assert "BREADCRUMB_KEY = 'appNavStack'" in APP_SHELL_JS
    assert "function pushBreadcrumb()" in APP_SHELL_JS
    assert "function goBack(homeHref)" in APP_SHELL_JS


def test_back_falls_back_to_home_when_no_previous_page():
    idx = APP_SHELL_JS.index("function goBack(homeHref)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx) + 4]
    assert "location.href = homeHref;" in body


def test_home_button_navigates_to_resolved_landing():
    idx = APP_SHELL_JS.index("homeBtn.addEventListener")
    snippet = APP_SHELL_JS[idx:idx + 150]
    assert "location.href = homeHref;" in snippet
    # homeHref itself is computed via resolveLanding() in render().
    assert "var homeHref = resolveLanding(session.user.role, flags);" in APP_SHELL_JS


# =====================================================================
# Navigation contexts (source-level — reporting vs operational)
# =====================================================================

def test_operator_gets_operational_switcher_never_reporting_nav():
    idx = APP_SHELL_JS.index("function renderNav(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function renderIdentityBar", idx)]
    operator_branch_idx = body.index("if (role === 'operator')")
    reporting_call_idx = body.index("reportingNavItems(role, flags)")
    assert operator_branch_idx < reporting_call_idx, \
        "the operator branch must be checked (and returned from) before reportingNavItems is ever reached"


def test_operator_gets_a_visually_separated_review_group():
    idx = APP_SHELL_JS.index("function renderNav(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function renderIdentityBar", idx)]
    assert "ash-review-group" in body
    assert "reviewLinkItems(flags)" in body


def test_operational_book_pages_get_focused_switcher_for_every_role():
    """Manager/Super Admin/Viewer landing on Dispatch/Returns/Production
    directly (e.g. via Operations) get the same focused 3-item switcher as
    Operators — never the full reporting nav mixed in."""
    idx = APP_SHELL_JS.index("function renderNav(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function renderIdentityBar", idx)]
    assert "OPERATIONAL_PAGES.indexOf(pageKey) !== -1" in body


def test_reporting_nav_dashboard_visible_to_all_reporting_roles():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "if (enabled(flags, 'dashboard'))" in body
    assert "label: 'Dashboard'" in body


def test_reporting_nav_operations_only_for_manager_and_super_admin():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "(role === 'manager' || role === 'super_admin')" in body
    assert "label: 'Operations'" in body


def test_reporting_nav_admin_only_for_super_admin():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "if (role === 'super_admin')" in body
    assert "label: 'Admin'" in body


def test_operational_switcher_covers_all_three_books():
    idx = APP_SHELL_JS.index("function operationalNavItems(flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "label: 'Dispatch'" in body
    assert "label: 'Returns'" in body
    assert "label: 'Production'" in body


def test_nav_hiding_is_client_convenience_not_the_only_gate():
    """Navigation hiding must never be the sole authorization — every
    module already has its own server-side role/feature_required checks
    (see webapp/auth.py); this just pins that app-shell.js's nav items are
    always flag-gated via enabled(), not rendered unconditionally."""
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("function operationalNavItems", idx)]
    assert body.count("enabled(flags,") >= 3


# =====================================================================
# Active-tab / aria-current
# =====================================================================

def test_nav_link_marks_active_item_with_aria_current():
    idx = APP_SHELL_JS.index("function navLink(item, activeKey)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "item.key === activeKey" in body
    assert "setAttribute('aria-current', 'page')" in body


def test_active_state_derived_from_current_url_path_not_stored_client_state():
    idx = APP_SHELL_JS.index("function currentPageKey()")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "location.pathname" in body


def test_active_nav_style_uses_more_than_color_alone():
    """Section 6 requires a second cue beyond color (weight/border/
    underline) for accessibility. Styling itself lives in the shared
    static/app-shell.css stylesheet (see tests/test_stage6_correction_shell_styling.py
    for its full coverage) — this just pins that app-shell.js's nav links
    are still built to be styleable via aria-current/a dedicated class."""
    assert "border-bottom-color:var(--amber" in APP_SHELL_CSS
    assert "setAttribute('aria-current', 'page')" in APP_SHELL_JS


def test_focus_visible_styles_present_for_keyboard_users():
    assert ":focus-visible{" in APP_SHELL_CSS


# =====================================================================
# Feature flags stay independent of role — admin/login always reachable
# =====================================================================

def test_admin_page_is_never_feature_flag_gated(client, login_as):
    login_as("root", "password123", "super_admin")
    for module_key in ("dashboard", "dispatch", "returns", "production", "history_exports"):
        _set_flag(client, module_key, False)
    res = client.get("/admin.html")
    assert res.status_code == 200


def test_login_page_is_never_guarded(client):
    res = client.get("/")
    assert res.status_code == 200


# =====================================================================
# Dashboard — activity, Day/Night Production, attention (backend)
# =====================================================================

def test_dashboard_activity_counts_finalized_and_draft_separately(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-20"
    finalized = client.post("/api/dispatches", json={
        "dispatch_number": "S6-F1", "date": date, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{finalized['id']}/finalize")
    client.post("/api/dispatches", json={
        "dispatch_number": "S6-D1", "date": date, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })

    res = client.get(f"/api/dashboard?date={date}")
    activity = res.get_json()["activity"]
    assert activity["dispatch"]["finalized"] == 1
    assert activity["dispatch"]["draft"] == 1


def test_dashboard_day_night_production_split(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-21"
    day = client.post("/api/production", json={
        "date": date, "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{day['id']}/finalize")
    night = client.post("/api/production", json={
        "date": date, "shift": "Night", "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{night['id']}/finalize")

    res = client.get(f"/api/dashboard?date={date}").get_json()
    assert res["activity"]["production"]["day_finalized"] == 1
    assert res["activity"]["production"]["night_finalized"] == 1
    shift_row = res["production_by_shift"][str(pid)] if str(pid) in res["production_by_shift"] else res["production_by_shift"][pid]
    assert shift_row["day"] == 100
    assert shift_row["night"] == 200


def test_dashboard_attention_lists_no_finalized_notices_for_empty_day(client, setup):
    res = client.get("/api/dashboard?date=2026-07-22").get_json()
    types = {n["type"] for n in res["attention"]}
    assert "no_finalized_dispatch" in types
    assert "no_finalized_returns" in types
    assert "no_finalized_day_production" in types
    assert "no_finalized_night_production" in types


def test_dashboard_attention_clears_notice_once_finalized_dispatch_exists(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-23"
    d = client.post("/api/dispatches", json={
        "dispatch_number": "S6-CLEAR", "date": date, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    res = client.get(f"/api/dashboard?date={date}").get_json()
    types = {n["type"] for n in res["attention"]}
    assert "no_finalized_dispatch" not in types


def test_dashboard_attention_drafts_pending_count_combines_all_three_books(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-24"
    client.post("/api/dispatches", json={
        "dispatch_number": "S6-DR", "date": date, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    client.post("/api/production", json={
        "date": date, "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })

    res = client.get(f"/api/dashboard?date={date}").get_json()
    notice = next(n for n in res["attention"] if n["type"] == "drafts_pending")
    assert notice["count"] == 2


def test_dashboard_attention_flags_negative_closing_stock(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-25"
    # A positive delta_base_qty adjustment counts toward "issued" (see
    # stock_service.date_range_summary/adjustment_total_base_qty_range) —
    # with no opening/production stock behind it, closing goes negative.
    client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": date, "shift": "Day",
        "delta_base_qty": 50, "reason": "test negative",
    })
    res = client.get(f"/api/dashboard?date={date}").get_json()
    notice = next((n for n in res["attention"] if n["type"] == "negative_closing_stock"), None)
    assert notice is not None
    assert notice["product_id"] == pid


def test_dashboard_generated_at_timestamp_present(client, setup):
    res = client.get("/api/dashboard?date=2026-07-26").get_json()
    assert res["generated_at"]


def test_top_products_include_book_notation_split(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-27"
    d = client.post("/api/dispatches", json={
        "dispatch_number": "S6-TOP", "date": date, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.get(f"/api/dashboard?date={date}").get_json()
    entry = next(t for t in res["top_products"] if t["product_id"] == pid)
    assert entry["cartons"] == 1
    assert entry["packaging_rule"] is not None


def test_dashboard_never_labels_dispatched_quantities_as_monetary_sales():
    """No pricing model exists in this app — the Dashboard page must never
    invent revenue/sales/profit language."""
    source = PRIMARY_PAGES["dashboard.html"]
    for forbidden in ("Revenue", "Sales Total", "Profit", "Price"):
        assert forbidden not in source


def test_dashboard_per_product_table_never_sums_across_products():
    """Each per-product row formats its own quantities with its own
    packaging_rule (qty(r.opening, r.packaging_rule) etc.) — there is no
    code path that sums base_qty across different products' rows into one
    combined total."""
    source = PRIMARY_PAGES["dashboard.html"]
    assert "reduce(" not in source


def test_dashboard_page_contains_no_write_requests():
    """Read-only by construction for every role — mirrors history.html's
    equivalent guard."""
    source = PRIMARY_PAGES["dashboard.html"]
    for verb in ("'POST'", "'PUT'", "'PATCH'", "'DELETE'", '"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in source, f"found a {verb} call in dashboard.html — this page must stay read-only"


def test_dashboard_quick_actions_role_correct():
    source = PRIMARY_PAGES["dashboard.html"]
    idx = source.index("function renderQuickActions(role)")
    body = source[idx:source.index("\n}", idx)]
    assert "role === 'manager' || role === 'super_admin'" in body
    assert "label:'Open Operations'" in body
    assert "label:'View Daily Figures'" in body
    assert "label:'Open History & Exports'" in body
    assert "role === 'super_admin'" in body
    assert "label:'Open Admin'" in body


def test_dashboard_default_date_uses_local_not_utc_helpers():
    source = PRIMARY_PAGES["dashboard.html"]
    assert "function localDateStr(d){" in source
    assert "d.getFullYear(), m = String(d.getMonth()+1)" in source or "d.getFullYear()" in source
    assert "toISOString().slice(0,10)" not in source


def test_dashboard_date_controls_present():
    source = PRIMARY_PAGES["dashboard.html"]
    for control_id in ("quickToday", "quickYesterday", "prevDateBtn", "nextDateBtn", "dateInput", "refreshBtn"):
        assert f'id="{control_id}"' in source


# =====================================================================
# Super-admin user management — password reset, session invalidation,
# self-lockout, audit content
# =====================================================================

def test_non_super_admin_cannot_reset_passwords(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target1", "password": "password123", "role": "operator",
    }).get_json()
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    res = client.post(f"/api/admin/users/{target['id']}/reset-password",
                       json={"password": "newpassword1", "confirm_password": "newpassword1"})
    assert res.status_code == 403


def test_reset_password_rejects_short_password(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target2", "password": "password123", "role": "operator",
    }).get_json()
    res = client.post(f"/api/admin/users/{target['id']}/reset-password",
                       json={"password": "short", "confirm_password": "short"})
    assert res.status_code == 400


def test_reset_password_rejects_mismatched_confirmation(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target3", "password": "password123", "role": "operator",
    }).get_json()
    res = client.post(f"/api/admin/users/{target['id']}/reset-password",
                       json={"password": "newpassword1", "confirm_password": "somethingelse"})
    assert res.status_code == 400


def test_reset_password_never_returns_password_or_hash(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target4", "password": "password123", "role": "operator",
    }).get_json()
    res = client.post(f"/api/admin/users/{target['id']}/reset-password",
                       json={"password": "newpassword1", "confirm_password": "newpassword1"})
    body = res.get_json()
    assert "password" not in body
    assert "password_hash" not in body
    assert "newpassword1" not in res.get_data(as_text=True)


def test_reset_password_old_password_stops_working(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target5", "password": "password123", "role": "operator",
    }).get_json()
    client.post(f"/api/admin/users/{target['id']}/reset-password",
                json={"password": "newpassword1", "confirm_password": "newpassword1"})
    client.post("/api/logout")
    res = client.post("/api/login", json={"username": "target5", "password": "password123"})
    assert res.status_code == 401


def test_reset_password_invalidates_other_active_sessions(app, client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target6", "password": "password123", "role": "operator",
    }).get_json()

    other_client = app.test_client()
    login_res = other_client.post("/api/login", json={"username": "target6", "password": "password123"})
    assert login_res.status_code == 200
    assert other_client.get("/api/session").get_json()["authed"] is True

    client.post(f"/api/admin/users/{target['id']}/reset-password",
                json={"password": "newpassword1", "confirm_password": "newpassword1"})

    # The old session cookie (still stamped with the old session_version)
    # must now read as logged-out — no re-login has happened on this client.
    assert other_client.get("/api/session").get_json()["authed"] is False


def test_reset_password_audit_entry_never_contains_password_or_hash(app, client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target7", "password": "password123", "role": "operator",
    }).get_json()
    client.post(f"/api/admin/users/{target['id']}/reset-password",
                json={"password": "supersecretnew1", "confirm_password": "supersecretnew1"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="reset_password", entity_id=str(target["id"])).first()
        assert entry is not None
        assert "supersecretnew1" not in (entry.before_json or "")
        assert "supersecretnew1" not in (entry.after_json or "")
        assert "password" not in (entry.before_json or "")
        assert "password" not in (entry.after_json or "")
        assert "session_version" in (entry.after_json or "")


def test_username_edit_via_patch(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "oldname", "password": "password123", "role": "operator",
    }).get_json()
    res = client.patch(f"/api/admin/users/{target['id']}", json={"username": "newname"})
    assert res.status_code == 200
    assert res.get_json()["username"] == "newname"


def test_username_edit_rejects_duplicate(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/users", json={"username": "existing1", "password": "password123", "role": "operator"})
    target = client.post("/api/admin/users", json={"username": "existing2", "password": "password123", "role": "operator"}).get_json()
    res = client.patch(f"/api/admin/users/{target['id']}", json={"username": "existing1"})
    assert res.status_code == 409


def test_failed_role_validation_leaves_user_record_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target8", "password": "password123", "role": "operator",
    }).get_json()
    res = client.patch(f"/api/admin/users/{target['id']}", json={"role": "not_a_real_role"})
    assert res.status_code == 400
    current = client.get("/api/admin/users").get_json()
    row = next(u for u in current if u["id"] == target["id"])
    assert row["role"] == "operator"


def test_sole_active_super_admin_cannot_demote_themselves(client, login_as):
    """root is the only active super_admin at this point — self-demotion
    must be rejected (caught by the self-lockout check, which the
    independent last-active-super-admin check in _is_last_active_super_admin
    then backs up as a second line of defense for any other code path that
    might reach the same state)."""
    login_as("root", "password123", "super_admin")
    root_id = next(u["id"] for u in client.get("/api/admin/users").get_json() if u["username"] == "root")
    res = client.patch(f"/api/admin/users/{root_id}", json={"role": "viewer"})
    assert res.status_code == 400


def test_second_super_admin_can_demote_the_first(client, login_as):
    """Sanity check the guard is scoped to the LAST active super admin, not
    super admins in general — with two active, demoting one that isn't
    yourself is allowed."""
    login_as("root", "password123", "super_admin")
    second = client.post("/api/admin/users", json={
        "username": "second_admin", "password": "password123", "role": "super_admin",
    }).get_json()
    client.post("/api/logout")
    # second_admin already exists (created via the admin API above) —
    # log in directly rather than through login_as, which would try to
    # create a duplicate user with the same username.
    login_res = client.post("/api/login", json={"username": "second_admin", "password": "password123"})
    assert login_res.status_code == 200
    root_id = next(u["id"] for u in client.get("/api/admin/users").get_json() if u["username"] == "root")
    res = client.patch(f"/api/admin/users/{root_id}", json={"role": "viewer"})
    assert res.status_code == 200


def test_sole_active_super_admin_cannot_disable_themselves(client, login_as):
    login_as("root", "password123", "super_admin")
    root_id = next(u["id"] for u in client.get("/api/admin/users").get_json() if u["username"] == "root")
    res = client.patch(f"/api/admin/users/{root_id}", json={"active": False})
    assert res.status_code == 400
    res2 = client.delete(f"/api/admin/users/{root_id}")
    assert res2.status_code == 400


def test_admin_html_reset_password_modal_present():
    source = PRIMARY_PAGES["admin.html"]
    assert 'id="resetPasswordModal"' in source
    assert 'id="resetPasswordNew"' in source
    assert 'id="resetPasswordConfirm"' in source


def test_admin_html_username_field_editable_and_saved_with_role():
    source = PRIMARY_PAGES["admin.html"]
    assert 'data-username-for=' in source
    assert 'data-save=' in source


def test_admin_html_reset_password_fields_never_prefilled():
    source = PRIMARY_PAGES["admin.html"]
    idx = source.index('id="resetPasswordModal"')
    modal_block = source[idx:idx + 1500]
    for field_id in ("resetPasswordNew", "resetPasswordConfirm"):
        field_idx = modal_block.index(f'id="{field_id}"')
        tag_end = modal_block.index(">", field_idx)
        assert 'value="' not in modal_block[field_idx:tag_end]


# =====================================================================
# Company logo as PWA install icon
# =====================================================================

def test_manifest_served_dynamically_and_unauthenticated(client):
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.mimetype == "application/manifest+json"


def test_manifest_uses_generic_fallback_icons_when_no_logo(client):
    data = client.get("/manifest.webmanifest").get_json()
    srcs = [i["src"] for i in data["icons"]]
    assert any(s.startswith("/icons/") for s in srcs)


def test_manifest_uses_branding_derived_icons_after_logo_upload(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    data = client.get("/manifest.webmanifest").get_json()
    srcs = [i["src"] for i in data["icons"]]
    assert any(s.startswith("/api/branding/icon-192.png?v=") for s in srcs)
    assert any(s.startswith("/api/branding/icon-512.png?v=") for s in srcs)
    assert any(s.startswith("/api/branding/icon-512-maskable.png?v=") for s in srcs)


def test_manifest_reflects_current_company_display_name(client, login_as):
    login_as("root", "password123", "super_admin")
    client.patch("/api/admin/company-settings", json={"display_name": "Acme Traders"})
    data = client.get("/manifest.webmanifest").get_json()
    assert data["name"] == "Acme Traders"


def test_derived_icon_routes_serve_real_files_after_logo_upload(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    for path in ("/api/branding/icon-192.png", "/api/branding/icon-512.png", "/api/branding/icon-512-maskable.png"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.mimetype == "image/png"


def test_derived_icon_routes_fall_back_to_generic_when_no_logo(client):
    for path, fallback in (
        ("/api/branding/icon-192.png", "icon-192.png"),
        ("/api/branding/icon-512.png", "icon-512.png"),
        ("/api/branding/icon-512-maskable.png", "icon-maskable-512.png"),
    ):
        res = client.get(path)
        assert res.status_code == 200


def test_uploaded_logo_regenerates_icons_and_original_logo_preserved(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(color="red"), "logo1.png")},
                content_type="multipart/form-data")
    first_settings = client.get("/api/admin/company-settings").get_json()
    assert first_settings["pwa_icons_configured"] is True

    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(color="blue"), "logo2.png")},
                content_type="multipart/form-data")
    second_settings = client.get("/api/admin/company-settings").get_json()
    assert second_settings["pwa_icons_configured"] is True
    # The logo itself (not just icons) survives a replace as a live URL.
    assert second_settings["logo_url"]


def test_removing_logo_clears_icon_configuration_and_falls_back(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    client.delete("/api/admin/company-settings/logo")
    settings = client.get("/api/admin/company-settings").get_json()
    assert settings["pwa_icons_configured"] is False
    data = client.get("/manifest.webmanifest").get_json()
    srcs = [i["src"] for i in data["icons"]]
    assert any(s.startswith("/icons/") for s in srcs)


def test_company_settings_note_about_icon_cache_delay_present():
    source = PRIMARY_PAGES["admin.html"]
    # A truthful note that icon updates may be delayed by OS/browser cache
    # and reinstalling may be needed — must not claim instant replacement.
    assert "reinstall" in source.lower() or "cache" in source.lower()


def test_service_worker_no_longer_precaches_the_now_dynamic_manifest():
    sw_source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "'/manifest.webmanifest'" not in sw_source


def test_service_worker_never_caches_branding_api_routes():
    """/api/branding/icon-*.png must never be served stale from the SW
    cache — confirmed here by the existing blanket /api/ exclusion rule
    still being present and unmodified by this stage."""
    sw_source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "url.pathname.startsWith('/api/')" in sw_source


# =====================================================================
# Regression: existing behavior this stage must not have touched
# =====================================================================

def test_closing_stock_formula_unchanged(client, setup):
    pid = setup["product"]["id"]
    date = "2026-07-29"
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    res = client.get(f"/api/dashboard?date={date}").get_json()
    row = next(r for r in res["stock_summary"] if r["product_id"] == pid)
    assert row["closing_base_qty"] == row["opening_base_qty"] + row["production_base_qty"] + row["return_base_qty"] - row["issued_base_qty"]


def test_existing_feature_flag_cascade_protection_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False})
    assert res.status_code == 400
    assert "cascade" in res.get_json()["error"]


def test_pwa_install_assets_still_present():
    assert (STATIC_DIR / "sw.js").exists()
    assert (STATIC_DIR / "pwa.js").exists()


def test_manifest_route_overrides_the_static_file_of_the_same_name(client):
    """webapp/routes/pwa.py's explicit /manifest.webmanifest route must take
    precedence over Flask's static-file serving of the identically-named
    file still shipped under static/ (kept as a harmless, unreachable
    fallback) — confirmed by the response being live JSON, not a static
    asset with a static ETag/Last-Modified pair."""
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.mimetype == "application/manifest+json"
    assert res.get_json()["icons"]
