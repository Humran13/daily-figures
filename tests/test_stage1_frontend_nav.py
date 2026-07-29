"""
Source-level regression guards for Stage 1's role-aware navigation. As
with the earlier dispatch.html quantity-input fixes, this project has no
JS/browser test runner, so the frontend behavior that can't be exercised
through the Flask test client (which page is shown after login, which nav
links render for which role) is pinned down by checking the shipped
source rather than simulating a browser.
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _operator_nav_block(source):
    match = re.search(r'<div class="tabs hidden" id="operatorNav">.*?</div>', source, re.DOTALL)
    assert match, "operatorNav block is missing"
    return match.group(0)


@pytest.mark.parametrize("source", [DISPATCH_HTML, INDEX_HTML], ids=["dispatch.html", "index.html"])
def test_operator_nav_has_exactly_three_items_in_order(source):
    block = _operator_nav_block(source)
    links = re.findall(r'>([^<]+)</a>', block)
    labels = [l.strip().replace("&amp;", "&") for l in links]
    assert labels == ["Dispatch", "Daily Figures", "History & Exports"]


@pytest.mark.parametrize("source", [DISPATCH_HTML, INDEX_HTML], ids=["dispatch.html", "index.html"])
def test_operator_nav_hidden_by_default(source):
    assert 'class="tabs hidden" id="operatorNav"' in source


def test_operator_nav_toggled_by_role_in_dispatch_html():
    assert "document.getElementById('operatorNav').classList.toggle('hidden', !isOperatorOrViewer)" in DISPATCH_HTML


def test_operator_nav_toggled_by_role_in_index_html():
    assert "document.getElementById('operatorNav').classList.toggle('hidden', !isOperatorOrViewer)" in INDEX_HTML


def test_dashboard_link_hidden_for_operator_and_viewer_in_dispatch_html():
    # Stage 4's feature-flag hardening moved this from a raw
    # classList.toggle() to the independent role/flag visibility
    # mechanism (setRoleVisible) — see tests/test_stage4_frontend.py for
    # the full behavior. This just pins that dashboardLink is still
    # role-gated by the same condition, through the new mechanism.
    assert "setRoleVisible(document.getElementById('dashboardLink'), ['super_admin','manager'].includes(data.user.role))" in DISPATCH_HTML


def test_dashboard_link_hidden_for_operator_and_viewer_in_index_html():
    assert "setRoleVisible(document.getElementById('dashboardLink'), ['super_admin','manager'].includes(user.role))" in INDEX_HTML


def test_operator_and_viewer_default_to_new_dispatch_tab():
    assert "const defaultTab = isOperatorOrViewer ? 'new' : 'list';" in DISPATCH_HTML


def test_operator_and_viewer_redirected_to_dispatch_after_login():
    assert "window.location.href = '/dispatch.html?tab=new';" in INDEX_HTML
    match = re.search(r"if\(data\.ok\)\{(.*?)\n  \}", INDEX_HTML, re.DOTALL)
    assert match, "attemptLogin's data.ok branch is missing"
    assert "['operator','viewer'].includes(data.user.role)" in match.group(1)


def test_manager_and_super_admin_nav_markup_unchanged_in_dispatch_html():
    """Stage 1 must not touch admin-tier's existing navigation elements."""
    assert '<div class="tab" data-tab="new">New Dispatch</div>' in DISPATCH_HTML
    assert '<div class="tab active" data-tab="list">Dispatches</div>' in DISPATCH_HTML
    assert 'id="dashboardLink"' in DISPATCH_HTML


def test_manager_and_super_admin_nav_markup_unchanged_in_index_html():
    assert '<div class="tab active" data-tab="entry">Enter</div>' in INDEX_HTML
    assert '<div class="tab" data-tab="history">History &amp; Export</div>' in INDEX_HTML
    assert 'id="adminLink"' in INDEX_HTML


def test_viewer_readonly_lock_present_on_dispatch_html():
    assert "function applyViewerReadOnly()" in DISPATCH_HTML
    assert "currentUser.role !== 'viewer'" in DISPATCH_HTML


def test_daily_figures_fields_gated_per_role_and_permission_flags():
    assert "const canEditOpening = isElevated || (role === 'operator' && operatorPermissions.can_edit_opening);" in INDEX_HTML
    assert "const canEditReturns = isElevated || (role === 'operator' && operatorPermissions.can_edit_returns);" in INDEX_HTML
    assert "const canEditProduction = isElevated || (role === 'operator' && operatorPermissions.can_edit_production);" in INDEX_HTML
    assert "const canAdjust = isElevated || (role === 'operator' && operatorPermissions.can_create_adjustments);" in INDEX_HTML


def test_viewer_forces_all_fields_disabled_regardless_of_flags():
    # canEdit*/canAdjust are keyed off isElevated / role==='operator' only —
    # 'viewer' never satisfies either, so it can never inherit a permission
    # flag. isFullyReadOnly is always true for Viewer independent of those
    # flags — see tests/test_stage1_ui_cleanup_readonly_daily_figures.py
    # and tests/test_stage1_correction_next_product_review.py for the full
    # read-only-navigation behavior this drives.
    assert "const isViewer = role === 'viewer';" in INDEX_HTML
    assert "const isFullyReadOnly = isViewer ||" in INDEX_HTML
