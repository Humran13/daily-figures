"""
Stage 4 frontend: [data-module] nav hiding across every page, and the
Feature Flags admin panel. Source-level regression guards, same rationale
as every other frontend-only piece of this project (no JS/browser test
runner exists here).

Stage 6 centralized cross-page navigation (Dashboard/Dispatch/History &
Exports links, role-based show/hide) into static/app-shell.js — see
tests/test_stage6_app_shell.py for that architecture's own coverage. Six
of the seven pages (index/dispatch/returns/production/history/admin) still
define their own applyFeatureFlags()/setRoleVisible()/setFlagVisible()
helpers from before that change; only dashboard.html was rewritten from
scratch and has none of them, relying entirely on app-shell.js instead.
Of the six, only history.html and admin.html still have real [data-module]
elements left for those helpers to act on (their own internal tabs/panels);
on index/dispatch/returns/production the helpers are harmless dead code
that now iterates over zero matching elements.
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
    "returns.html": (STATIC_DIR / "returns.html").read_text(encoding="utf-8"),
    "production.html": (STATIC_DIR / "production.html").read_text(encoding="utf-8"),
}

# dashboard.html was fully rewritten in Stage 6 and no longer defines any of
# its own feature-flag/visibility machinery — app-shell.js does it for the
# whole page (identity bar + nav). Every other page still defines it, even
# where (as on index/dispatch/returns/production) it's now dead code.
PAGES_WITH_OWN_FLAG_MACHINERY = {
    name: src for name, src in PAGES.items() if name != "dashboard.html"
}

APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")


def test_every_page_defines_apply_feature_flags():
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
        assert "async function applyFeatureFlags()" in source, f"{name} is missing applyFeatureFlags()"


def test_dashboard_html_relies_on_app_shell_for_feature_flags():
    """Stage 6: dashboard.html has no feature-flag machinery of its own —
    static/app-shell.js fetches /api/feature-flags once and renders the
    role/flag-aware nav for it (and every other page)."""
    assert "async function applyFeatureFlags()" not in PAGES["dashboard.html"]
    assert "/api/feature-flags" in APP_SHELL_JS
    assert '<script src="/app-shell.js" defer></script>' in PAGES["dashboard.html"]


def test_every_page_fetches_feature_flags_endpoint():
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
        assert "/api/feature-flags" in source, f"{name} never calls /api/feature-flags"


def test_every_page_defines_the_independent_visibility_helpers():
    """Role-based visibility and feature-flag visibility are tracked as
    two separate pieces of state on each element (dataset.roleVisible /
    dataset.flagVisible), combined by AND — never a single shared 'hidden'
    class fought over by two different call sites."""
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
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
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
        idx = source.index("async function applyFeatureFlags()")
        snippet = source[idx:source.index("\n}", idx) + 2]
        assert "setFlagVisible(el, featureFlagsCache[el.dataset.module] !== false)" in snippet or \
               "setFlagVisible(el, flags[el.dataset.module] !== false)" in snippet, \
               f"{name}'s applyFeatureFlags must call setFlagVisible(el, enabled), reflecting the latest fetch both ways"


def test_app_shell_renders_dashboard_and_dispatch_links_with_role_awareness():
    """Stage 6 moved the old per-page dashboardLink/dispatchLink role-toggle
    sites (previously driven by setRoleVisible) into the shared, centralized
    nav renderer — every page's Dashboard/Dispatch link now comes from one
    place instead of being duplicated (and independently toggled) seven
    times. See tests/test_stage6_app_shell.py for resolveLanding()/nav-item
    role-filtering coverage."""
    assert "/dashboard.html" in APP_SHELL_JS
    assert "/dispatch.html" in APP_SHELL_JS
    assert "aria-current" in APP_SHELL_JS


def test_set_flag_visible_never_touches_role_state():
    """Role hiding must never be undone by a feature-flag update: the flag
    setter only ever writes dataset.flagVisible, never dataset.roleVisible."""
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
        idx = source.index("function setFlagVisible(el, visible){")
        body = source[idx:source.index("}", idx) + 1]
        assert "roleVisible" not in body, f"{name}'s setFlagVisible must not touch roleVisible"


def test_set_role_visible_never_touches_flag_state():
    # admin.html never defines setRoleVisible() — every page reachable there
    # is already super_admin-only, so it has nothing left to role-gate at
    # the element level (only feature-flag gating, via setFlagVisible).
    for name in ("index.html", "dispatch.html", "history.html", "returns.html", "production.html"):
        source = PAGES[name]
        assert "function setRoleVisible(el, visible){" in source, f"{name} is missing setRoleVisible()"
        idx = source.index("function setRoleVisible(el, visible){")
        body = source[idx:source.index("}", idx) + 1]
        assert "flagVisible" not in body, f"{name}'s setRoleVisible must not touch flagVisible"


def test_apply_feature_flags_fetch_failure_never_throws_uncaught():
    for name, source in PAGES_WITH_OWN_FLAG_MACHINERY.items():
        idx = source.index("async function applyFeatureFlags()")
        snippet = source[idx:idx + 1200]
        assert "try{" in snippet and "catch(e)" in snippet, f"{name}'s applyFeatureFlags() must not let a failed fetch throw"


# ---------- per-page data-module tagging ----------
# Only history.html and admin.html still have real [data-module] HTML
# elements after Stage 6 — index/dispatch/returns/production/dashboard's
# old dashboardLink/dispatchLink/cross-module tab links were all replaced
# by the shared nav in app-shell.js (see test above).

def test_index_html_no_longer_hardcodes_dashboard_or_dispatch_nav_links():
    source = PAGES["index.html"]
    assert 'id="dashboardLink"' not in source
    assert 'id="dispatchLink"' not in source
    assert 'data-module="' not in source


def test_index_html_redirects_away_when_daily_figures_disabled():
    source = PAGES["index.html"]
    assert "featureFlagsCache.daily_figures === false" in source
    assert "window.location.href = '/dispatch.html'" in source
    assert "window.location.href = '/history.html'" in source


def test_dispatch_html_no_longer_hardcodes_cross_module_nav_links():
    source = PAGES["dispatch.html"]
    assert 'id="dashboardLink"' not in source
    assert 'data-module="' not in source


def test_history_html_tags_internal_tabs():
    source = PAGES["history.html"]
    assert 'data-tab="dispatch" data-module="dispatch"' in source
    assert 'data-tab="daily-figures" data-module="daily_figures"' in source
    # The old header dashboardLink was removed in favor of app-shell.js's
    # shared identity bar / reporting nav.
    assert 'id="dashboardLink"' not in source


def test_history_html_switches_away_from_a_hidden_active_tab():
    source = PAGES["history.html"]
    assert "activeTab.classList.contains('hidden')" in source
    assert "switchTab(other.dataset.tab)" in source


def test_dashboard_html_uses_shared_identity_and_nav_placeholders():
    source = PAGES["dashboard.html"]
    assert 'id="appIdentityBar"' in source
    assert 'id="appRoleNav"' in source
    # The old hard-coded home/dispatch links tied to setRoleVisible are gone.
    assert 'data-module="' not in source


def test_admin_html_tags_customer_management_tabs():
    source = PAGES["admin.html"]
    for panel in ("customers", "categories", "review", "import"):
        assert f'data-panel="{panel}" data-module="customer_management"' in source
    # users/products/legacy/company/flags are never module-gated
    for panel in ("users", "products", "legacy", "company", "flags"):
        assert f'data-panel="{panel}" data-module' not in source
    # The old header dashboardLink was removed in favor of app-shell.js.
    assert 'id="dashboardLink"' not in source


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


def _load_feature_flags_admin_snippet(source):
    idx = source.index("async function loadFeatureFlagsAdmin()")
    end = source.index("\n// ---------- boot ----------", idx)
    return source[idx:end]


def test_admin_feature_flag_toggle_handles_cascade_required_error():
    snippet = _load_feature_flags_admin_snippet(PAGES["admin.html"])
    assert "cascade: true" in snippet
    assert "confirm(data.error" in snippet  # native confirm() dialog, not a silent auto-proceed


def test_admin_refreshes_own_nav_after_a_successful_flag_toggle():
    snippet = _load_feature_flags_admin_snippet(PAGES["admin.html"])
    assert snippet.count("applyFeatureFlags();") >= 2  # once for the plain success path, once for the cascade-confirmed retry


def test_admin_refreshes_shared_app_shell_nav_after_a_flag_toggle():
    """Stage 6: a flag toggle from the Admin page must also refresh the
    shared identity bar / role nav rendered by app-shell.js (e.g. Dashboard
    disappearing from the nav the instant dashboard is disabled), not just
    admin.html's own internal panel tabs."""
    snippet = _load_feature_flags_admin_snippet(PAGES["admin.html"])
    assert snippet.count("window.AppShell.refresh();") >= 2
