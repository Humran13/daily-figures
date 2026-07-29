"""
Stage 4 frontend: [data-module] nav hiding across every page, and the
Feature Flags admin panel. Source-level regression guards, same rationale
as every other frontend-only piece of this project (no JS/browser test
runner exists here).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PAGES = {
    "index.html": (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
    "dispatch.html": (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8"),
    "history.html": (STATIC_DIR / "history.html").read_text(encoding="utf-8"),
    "dashboard.html": (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"),
    "admin.html": (STATIC_DIR / "admin.html").read_text(encoding="utf-8"),
}


def test_every_page_defines_apply_feature_flags():
    for name, source in PAGES.items():
        assert "async function applyFeatureFlags()" in source, f"{name} is missing applyFeatureFlags()"


def test_every_page_fetches_feature_flags_endpoint():
    for name, source in PAGES.items():
        assert "/api/feature-flags" in source, f"{name} never calls /api/feature-flags"


def test_every_page_defines_the_independent_visibility_helpers():
    """Role-based visibility and feature-flag visibility are tracked as
    two separate pieces of state on each element (dataset.roleVisible /
    dataset.flagVisible), combined by AND — never a single shared 'hidden'
    class fought over by two different call sites."""
    for name, source in PAGES.items():
        assert "function _recomputeVisibility(el)" in source, f"{name} is missing _recomputeVisibility()"
        assert "function setFlagVisible(el, visible)" in source, f"{name} is missing setFlagVisible()"
        assert "el.dataset.roleVisible !== 'false'" in source, f"{name}'s recompute must default role-visible to true"
        assert "el.dataset.flagVisible !== 'false'" in source, f"{name}'s recompute must default flag-visible to true"
        assert "!(roleOk && flagOk)" in source, f"{name} must hide when EITHER role or flag says no (AND, not OR)"


def test_apply_feature_flags_uses_set_flag_visible_not_raw_classlist():
    """applyFeatureFlags() must route every [data-module] element through
    setFlagVisible (which can both hide AND re-show), not touch
    classList.add/toggle('hidden', ...) directly — that's exactly what
    caused a re-enabled module to stay hidden forever within the same
    session."""
    for name, source in PAGES.items():
        idx = source.index("async function applyFeatureFlags()")
        snippet = source[idx:source.index("\n}", idx) + 2]
        assert "setFlagVisible(el, featureFlagsCache[el.dataset.module] !== false)" in snippet or \
               "setFlagVisible(el, flags[el.dataset.module] !== false)" in snippet, \
               f"{name}'s applyFeatureFlags must call setFlagVisible(el, enabled), reflecting the latest fetch both ways"


def test_role_toggle_sites_for_dashboard_and_dispatch_links_use_set_role_visible():
    """dashboardLink (index/dispatch/history) and dispatchLink (index) are
    both role-gated AND module-gated at the same element — these specific
    sites must go through setRoleVisible, not a raw classList.toggle,
    which is exactly what silently fought with the flag-based hide/show
    before this fix."""
    for name in ("index.html", "dispatch.html", "history.html"):
        source = PAGES[name]
        assert "setRoleVisible(document.getElementById('dashboardLink')," in source, \
            f"{name}'s dashboardLink role-toggle must use setRoleVisible"
    source = PAGES["index.html"]
    assert "setRoleVisible(document.getElementById('dispatchLink')," in source


def test_set_flag_visible_never_touches_role_state():
    """Role hiding must never be undone by a feature-flag update: the flag
    setter only ever writes dataset.flagVisible, never dataset.roleVisible."""
    for name, source in PAGES.items():
        idx = source.index("function setFlagVisible(el, visible){")
        body = source[idx:source.index("}", idx) + 1]
        assert "roleVisible" not in body, f"{name}'s setFlagVisible must not touch roleVisible"


def test_set_role_visible_never_touches_flag_state():
    for name in ("index.html", "dispatch.html", "history.html", "dashboard.html"):
        source = PAGES[name]
        assert "function setRoleVisible(el, visible){" in source, f"{name} is missing setRoleVisible()"
        idx = source.index("function setRoleVisible(el, visible){")
        body = source[idx:source.index("}", idx) + 1]
        assert "flagVisible" not in body, f"{name}'s setRoleVisible must not touch flagVisible"


def test_apply_feature_flags_fetch_failure_never_throws_uncaught():
    for name, source in PAGES.items():
        idx = source.index("async function applyFeatureFlags()")
        snippet = source[idx:idx + 1200]
        assert "try{" in snippet and "catch(e)" in snippet, f"{name}'s applyFeatureFlags() must not let a failed fetch throw"


# ---------- per-page data-module tagging ----------

def test_index_html_tags_dispatch_dashboard_and_history_exports_links():
    source = PAGES["index.html"]
    assert 'id="dashboardLink" data-module="dashboard"' in source
    assert 'id="dispatchLink" data-module="dispatch"' in source
    assert 'href="/history.html" class="tab" data-module="history_exports"' in source


def test_index_html_redirects_away_when_daily_figures_disabled():
    source = PAGES["index.html"]
    assert "featureFlagsCache.daily_figures === false" in source
    assert "window.location.href = '/dispatch.html'" in source
    assert "window.location.href = '/history.html'" in source


def test_dispatch_html_tags_dashboard_and_other_module_links():
    source = PAGES["dispatch.html"]
    assert 'id="dashboardLink" data-module="dashboard"' in source
    assert 'href="/" class="tab" data-module="daily_figures"' in source
    assert 'href="/history.html" class="tab" data-module="history_exports"' in source


def test_history_html_tags_internal_tabs_and_cross_links():
    source = PAGES["history.html"]
    assert 'data-tab="dispatch" data-module="dispatch"' in source
    assert 'data-tab="daily-figures" data-module="daily_figures"' in source
    assert 'id="dashboardLink" data-module="dashboard"' in source


def test_history_html_switches_away_from_a_hidden_active_tab():
    source = PAGES["history.html"]
    assert "activeTab.classList.contains('hidden')" in source
    assert "switchTab(other.dataset.tab)" in source


def test_dashboard_html_tags_daily_figures_and_dispatch_links():
    source = PAGES["dashboard.html"]
    assert 'class="home" data-module="daily_figures"' in source
    assert 'href="/dispatch.html" data-module="dispatch"' in source


def test_admin_html_tags_dashboard_link_and_customer_management_tabs():
    source = PAGES["admin.html"]
    assert 'id="dashboardLink" data-module="dashboard"' in source
    for panel in ("customers", "categories", "review", "import"):
        assert f'data-panel="{panel}" data-module="customer_management"' in source
    # users/products/legacy/company/flags are never module-gated
    for panel in ("users", "products", "legacy", "company", "flags"):
        assert f'data-panel="{panel}" data-module' not in source


# ---------- admin.html Feature Flags panel ----------

def test_admin_has_feature_flags_tab_and_panel():
    source = PAGES["admin.html"]
    assert '<div class="tab" data-panel="flags">Feature Flags</div>' in source
    assert 'id="panel-flags"' in source
    assert 'id="flagsTable2"' in source  # distinct from the pre-existing legacy-data #flagsTable


def test_admin_loads_feature_flags_on_boot():
    source = PAGES["admin.html"]
    assert "loadFeatureFlagsAdmin()" in source
    boot_idx = source.index("// ---------- boot ----------")
    assert "loadFeatureFlagsAdmin()" in source[boot_idx:]


def test_admin_feature_flag_toggle_handles_cascade_required_error():
    source = PAGES["admin.html"]
    idx = source.index("async function loadFeatureFlagsAdmin()")
    snippet = source[idx:idx + 2000]
    assert "cascade: true" in snippet
    assert "confirm(data.error" in snippet  # native confirm() dialog, not a silent auto-proceed


def test_admin_refreshes_own_nav_after_a_successful_flag_toggle():
    source = PAGES["admin.html"]
    idx = source.index("async function loadFeatureFlagsAdmin()")
    snippet = source[idx:idx + 3000]
    assert snippet.count("applyFeatureFlags();") >= 2  # once for the plain success path, once for the cascade-confirmed retry
