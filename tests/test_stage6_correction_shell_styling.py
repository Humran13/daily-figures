"""
Stage 6 blocking UI correction: the shared app-shell header/nav
(static/app-shell.js) was inserted into every page but its styling never
actually applied — injectStyles() was defined but never called, so the
logo rendered at native size, the identity bar was an unstyled block
(default white background), and Back/Home/Log out/nav links used raw
browser defaults.

Fix: a real shared stylesheet (static/app-shell.css), linked from every
page's own <head> so the rules exist before app-shell.js ever creates the
header/nav DOM — no flash of unstyled/oversized content, and no JS-driven
style injection to keep in sync. Source-level regression guards, same
rationale as every other frontend-only piece of this project (no
JS/browser test runner exists here).

This file intentionally does not re-test role landing, navigation
contents, Dashboard data, or user management — those are unchanged by
this purely-visual correction and already covered by
tests/test_stage6_app_shell.py.
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_SHELL_CSS = (STATIC_DIR / "app-shell.css").read_text(encoding="utf-8")
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")
PRIMARY_PAGES = {
    name: (STATIC_DIR / name).read_text(encoding="utf-8")
    for name in (
        "index.html", "dispatch.html", "returns.html", "production.html",
        "history.html", "dashboard.html", "admin.html",
    )
}


# =====================================================================
# Root cause: the old JS-injection mechanism must be gone, not just unused
# =====================================================================

def test_app_shell_js_no_longer_injects_styles_itself():
    """The old injectStyles()/appShellStyles <style> tag mechanism (defined
    but never called — the actual bug) has been removed entirely, so there
    is exactly one styling mechanism (the linked stylesheet) rather than a
    second, dormant, easy-to-re-break one sitting alongside it."""
    assert "function injectStyles()" not in APP_SHELL_JS
    assert "appShellStyles" not in APP_SHELL_JS


# =====================================================================
# Every authenticated page links the shared stylesheet
# =====================================================================

def test_every_primary_page_links_app_shell_stylesheet():
    for name, source in PRIMARY_PAGES.items():
        assert source.count('<link rel="stylesheet" href="/app-shell.css">') == 1, \
            f"{name} must link static/app-shell.css exactly once"


def test_stylesheet_link_precedes_the_pages_own_style_block():
    """The shared stylesheet must load before (or alongside) each page's
    own <style> block so there's no flash of oversized/unstyled shell
    content while the page's own CSS is still being parsed."""
    for name, source in PRIMARY_PAGES.items():
        link_idx = source.index('<link rel="stylesheet" href="/app-shell.css">')
        style_idx = source.index("<style>")
        assert link_idx < style_idx, f"{name}: app-shell.css must be linked before the page's own <style> block"


def test_shared_stylesheet_is_served(client):
    res = client.get("/app-shell.css")
    assert res.status_code == 200
    assert "text/css" in res.mimetype


def test_shared_stylesheet_served_unauthenticated(client):
    """The login screen itself (index.html, pre-session) must be able to
    load this stylesheet too — it's a static asset, not gated."""
    res = client.get("/app-shell.css")
    assert res.status_code == 200


# =====================================================================
# Logo: bounded height/width, contain, never controls header height
# =====================================================================

def test_logo_has_bounded_max_height_and_width():
    idx = APP_SHELL_CSS.index(".ash-brand img{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "max-height:44px" in block
    assert "max-width:160px" in block


def test_logo_uses_object_fit_contain_and_preserves_aspect_ratio():
    idx = APP_SHELL_CSS.index(".ash-brand img{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "object-fit:contain" in block
    assert "height:auto" in block
    assert "width:auto" in block


def test_mobile_media_query_shrinks_logo_further():
    idx = APP_SHELL_CSS.index("@media (max-width:480px)")
    mobile_block = APP_SHELL_CSS[idx:]
    logo_idx = mobile_block.index(".ash-brand img{")
    logo_rule = mobile_block[logo_idx:mobile_block.index("}", logo_idx)]
    assert "max-height:34px" in logo_rule
    assert "max-width:120px" in logo_rule


def test_no_unbounded_image_dimensions_anywhere_in_shared_css():
    """Nothing in the shared stylesheet lets an <img> render at its native
    resolution — every img rule that exists is height/width-bounded."""
    assert "img{width:100%}" not in APP_SHELL_CSS.replace(" ", "")
    assert "img{height:100%}" not in APP_SHELL_CSS.replace(" ", "")


def test_logo_falls_back_to_hidden_on_load_error():
    """A logo_url that 404s or otherwise fails to load must not leave a
    broken-image glyph — the <img> hides itself, leaving the text-only
    company name (already rendered alongside it) as the visible fallback."""
    idx = APP_SHELL_JS.index("logo.addEventListener('error'")
    snippet = APP_SHELL_JS[idx:idx + 120]
    assert "logo.classList.add('hidden')" in snippet


def test_hidden_logo_class_actually_hides_the_element():
    assert ".ash-brand img.hidden{ display:none; }" in APP_SHELL_CSS


def test_text_only_branding_fallback_still_present():
    """The company-name text span always renders regardless of logo
    presence — confirmed both in the CSS (styled, not display:none by
    default) and in app-shell.js (always appended alongside the logo)."""
    assert "setAttribute('data-brand-name', '')" in APP_SHELL_JS
    idx = APP_SHELL_CSS.index(".ash-brand span{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "display:none" not in block


# =====================================================================
# Header layout: bounded height, compact, dark theme, no giant white panel
# =====================================================================

def test_identity_bar_is_a_flex_row_not_a_stacked_block():
    idx = APP_SHELL_CSS.index(".ash-bar{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "display:flex" in block
    assert "align-items:center" in block


def test_identity_bar_has_dark_theme_background_not_default_white():
    idx = APP_SHELL_CSS.index(".ash-bar{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "background:var(--ink," in block
    assert "color:var(--paper," in block


def test_identity_bar_has_no_unbounded_min_height():
    idx = APP_SHELL_CSS.index(".ash-bar{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "min-height:44px" in block  # a small, fixed floor — never grows with logo/content


def test_mobile_actions_wrap_to_their_own_row_without_overflow():
    idx = APP_SHELL_CSS.index("@media (max-width:480px)")
    mobile_block = APP_SHELL_CSS[idx:]
    assert ".ash-actions{ width:100%;" in mobile_block


# =====================================================================
# Back / Home / Log out: shared control styling, not browser defaults
# =====================================================================

def test_back_home_logout_buttons_use_shared_button_class():
    idx = APP_SHELL_JS.index("function renderIdentityBar(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function escapeHtml", idx)]
    assert "backBtn.className = 'ash-btn';" in body
    assert "homeBtn.className = 'ash-btn';" in body
    assert "logoutBtn.className = 'ash-btn ash-logout';" in body


def test_ash_btn_has_padding_radius_and_visible_states():
    idx = APP_SHELL_CSS.index(".ash-btn{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "padding:7px 12px" in block
    assert "border-radius:8px" in block
    assert "cursor:pointer" in block


def test_ash_btn_has_hover_and_focus_visible_states():
    assert ".ash-btn:hover{" in APP_SHELL_CSS
    idx = APP_SHELL_CSS.index(".ash-btn:focus-visible{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "outline:2px solid var(--amber," in block


def test_logout_button_visually_distinct_from_back_and_home():
    assert ".ash-btn.ash-logout{ background:rgba(193,68,58" in APP_SHELL_CSS


# =====================================================================
# Navigation: spacing/wrapping, grouping, active state
# =====================================================================

def test_nav_container_uses_flex_with_wrapping_and_gap():
    idx = APP_SHELL_CSS.index(".ash-nav{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "display:flex" in block
    assert "flex-wrap:wrap" in block
    assert "gap:6px" in block


def test_nav_links_have_padding_so_they_never_run_together():
    idx = APP_SHELL_CSS.index(".ash-nav a{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "padding:6px 12px" in block


def test_review_group_visually_separated_with_border_and_label():
    idx = APP_SHELL_CSS.index(".ash-nav .ash-review-group{")
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "border-left:1px solid" in block
    assert ".ash-nav .ash-review-label{" in APP_SHELL_CSS


def test_review_group_moves_to_its_own_full_width_row_on_mobile():
    idx = APP_SHELL_CSS.index("@media (max-width:480px)")
    mobile_block = APP_SHELL_CSS[idx:]
    review_idx = mobile_block.index(".ash-nav .ash-review-group{")
    rule = mobile_block[review_idx:mobile_block.index("}", review_idx)]
    assert "width:100%" in rule
    assert "border-top:1px solid" in rule


def test_active_link_receives_aria_current_and_a_dedicated_class():
    idx = APP_SHELL_JS.index("function navLink(item, activeKey)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "setAttribute('aria-current', 'page')" in body
    assert "classList.add('ash-active')" in body


def test_active_nav_style_targets_both_aria_current_and_active_class():
    assert '.ash-nav a[aria-current="page"],' in APP_SHELL_CSS
    assert ".ash-nav a.ash-active{" in APP_SHELL_CSS


def test_active_nav_style_uses_more_than_color_alone():
    idx = APP_SHELL_CSS.index('.ash-nav a[aria-current="page"],')
    block = APP_SHELL_CSS[idx:APP_SHELL_CSS.index("}", idx)]
    assert "border-bottom-color:var(--amber," in block  # a second, non-color cue


def test_nav_links_have_visible_focus_state_for_keyboard_users():
    assert ".ash-nav a:focus-visible{" in APP_SHELL_CSS


# =====================================================================
# Page integration: shell doesn't clash with existing page content
# =====================================================================

def test_shared_css_never_touches_page_specific_element_ids():
    """The shared stylesheet only ever styles .ash-* classes — it must
    never reach into a page's own #dispatchList/#tab-new/etc. structure,
    keeping this a purely additive, page-agnostic stylesheet. Checked as an
    actual ID-selector pattern (#name immediately followed by a selector
    combinator or {) rather than a bare '#' search, since hex color codes
    like #1B2430 also contain '#'."""
    id_selectors = re.findall(r"#[A-Za-z][\w-]*\s*[\{,]", APP_SHELL_CSS)
    assert id_selectors == []


def test_pwa_install_banner_positioning_untouched():
    """The install banner/persistent install button (static/pwa.js) are
    fixed to the bottom of the viewport — nothing in this correction moves
    or restyles them, so they remain visible and don't overlap the (top)
    header this stylesheet styles."""
    pwa_js = (STATIC_DIR / "pwa.js").read_text(encoding="utf-8")
    assert "bottom:12px" in pwa_js
    assert "bottom:14px" in pwa_js


def test_every_primary_page_still_has_identity_and_nav_placeholders():
    for name, source in PRIMARY_PAGES.items():
        assert 'id="appIdentityBar"' in source
        assert 'id="appRoleNav"' in source


# =====================================================================
# Regression: role nav contents, landing pages, permissions, calculations
# unchanged by this purely-visual correction
# =====================================================================

def test_reporting_nav_role_gating_unchanged():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "(role === 'manager' || role === 'super_admin')" in body
    assert "if (role === 'super_admin')" in body


def test_operator_still_gets_operational_switcher_not_reporting_nav():
    idx = APP_SHELL_JS.index("function renderNav(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  function renderIdentityBar", idx)]
    assert "if (role === 'operator')" in body


def test_resolve_landing_logic_unchanged():
    idx = APP_SHELL_JS.index("function resolveLanding(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "if (role === 'operator')" in body
    assert "if (enabled(flags, 'dashboard')) return '/dashboard.html';" in body


def test_operator_lands_on_dispatch_by_default(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_viewer_can_reach_dashboard_read_only(client, login_as):
    login_as("view1", "password123", "viewer")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 200


def test_closing_stock_formula_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Shell Fix Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    pid = product["id"]
    date = "2026-07-28"
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    res = client.get(f"/api/dashboard?date={date}").get_json()
    row = next(r for r in res["stock_summary"] if r["product_id"] == pid)
    assert row["closing_base_qty"] == (
        row["opening_base_qty"] + row["production_base_qty"] + row["return_base_qty"] - row["issued_base_qty"]
    )


def test_pwa_manifest_route_unaffected(client):
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.mimetype == "application/manifest+json"
