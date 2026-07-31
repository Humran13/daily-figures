"""
Source-level regression guards for role-aware navigation. As with the
earlier dispatch.html quantity-input fixes, this project has no JS/browser
test runner, so the frontend behavior that can't be exercised through the
Flask test client (which page is shown after login, which nav links render
for which role) is pinned down by checking the shipped source rather than
simulating a browser.

Stage 6 replaced the old per-page operatorNav/dashboardLink/adminLink/
primaryTabs-role-toggle markup with one shared, centralized navigation
renderer (static/app-shell.js) loaded identically by every page — see
tests/test_stage6_app_shell.py for that architecture's own coverage. What
remains here is what Stage 6 explicitly did NOT change: the entry-wizard's
per-field role/permission gating, and dispatch.html's own internal-tab
default and read-only lock, neither of which is part of navigation.
"""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_operator_and_viewer_default_to_new_dispatch_tab():
    assert "const defaultTab = isOperatorOrViewer ? 'new' : 'list';" in DISPATCH_HTML


def test_viewer_readonly_lock_present_on_dispatch_html():
    assert "function applyViewerReadOnly()" in DISPATCH_HTML
    assert "currentUser.role !== 'viewer'" in DISPATCH_HTML


def test_daily_figures_fields_gated_per_role_and_permission_flags():
    # Stage 5 moved Return/Production entry to their own Books — Daily
    # Figures only ever displays them now (exactly like Issued already was),
    # so canEditReturns/canEditProduction no longer exist here; only Opening
    # Stock and the stock-adjustment action remain gated this way.
    # Stage 7 additionally gates the Operator branch on entry ownership
    # (!isCompleted && !blockedByOther) — Manager/Super Admin (isElevated)
    # remain unconditional.
    assert "const canEditOpening = isElevated || (role === 'operator' && operatorPermissions.can_edit_opening && !isCompleted && !blockedByOther);" in INDEX_HTML
    assert "const canEditReturns" not in INDEX_HTML
    assert "const canEditProduction" not in INDEX_HTML
    assert "const canAdjust = isElevated || (role === 'operator' && operatorPermissions.can_create_adjustments && !isCompleted && !blockedByOther);" in INDEX_HTML


def test_viewer_forces_all_fields_disabled_regardless_of_flags():
    # canEdit*/canAdjust are keyed off isElevated / role==='operator' only —
    # 'viewer' never satisfies either, so it can never inherit a permission
    # flag. isFullyReadOnly is always true for Viewer independent of those
    # flags — see tests/test_stage1_ui_cleanup_readonly_daily_figures.py
    # and tests/test_stage1_correction_next_product_review.py for the full
    # read-only-navigation behavior this drives.
    assert "const isViewer = role === 'viewer';" in INDEX_HTML
    assert "const isFullyReadOnly = isViewer ||" in INDEX_HTML


def test_index_html_no_longer_hardcodes_a_post_login_redirect():
    """Stage 6: every role now lands on its own role-aware page after
    login (see static/app-shell.js's resolveLanding()) — index.html's own
    login handler no longer hardcodes Operator/Viewer straight to
    Dispatch, or leaves Manager/Super Admin on Daily Figures itself."""
    assert "window.location.href = '/dispatch.html?tab=new';" not in INDEX_HTML
    assert "AppShell.resolveLanding" in INDEX_HTML
