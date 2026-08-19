"""
Final Dashboard UX correction — compact executive figures view. Source-
level regression guards (no JS/browser test runner exists in this
project — see every other frontend-only stage's tests for the same
pattern) proving:

- The required section order (Quick Actions, Daily Activity, Per-Product
  Daily Figures, Top Issued Products, Sales Category, Recipient,
  Attention, Recent Dispatches, Recent Corrections). The "Unfinalized
  Drafts" section (formerly between Recent Dispatches and Recent
  Corrections) was removed as an obsolete UI-only summary — see
  tests/test_final_ux_reporting_data_entry_package.py's own section for
  the removal itself; backend support (`draft_dispatches` in GET /api/
  dashboard, `_drafts_view()`) is deliberately left in place, unread by
  any current UI, per "prefer leaving harmless backend compatibility in
  place unless removing it is clearly safe."
- The three-item preview rule and its "View all" escape hatch.
- One reusable modal/drawer, not seven separate implementations.
- Attention collapsed by default, with a critical-condition auto-expand.
- Role-gated Quick Actions (Viewer/Manager/Super Administrator).

API-level behavior (daily_figures_today filtering, carton notation,
mixed-product grouping) is covered by
tests/test_final_dashboard_ux_daily_figures_today.py and
tests/test_final_dashboard_carton_notation.py — both still pass
unmodified, proving this presentation-only change didn't touch any
calculation.
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


# =====================================================================
# Required section order
# =====================================================================

SECTION_MARKERS = [
    'id="quickActions"',
    'id="activityTitle"',
    'id="dailyFiguresPreview"',
    'id="topProducts"',
    'id="byCategory"',
    'id="byRecipient"',
    'id="attentionCard"',
    'id="recentDispatches"',
    'id="correctionsList"',
]


def test_quick_actions_appear_first():
    assert DASHBOARD_HTML.index('id="quickActions"') < DASHBOARD_HTML.index('id="activityTitle"')


def test_daily_activity_follows_quick_actions():
    assert DASHBOARD_HTML.index('id="activityTitle"') < DASHBOARD_HTML.index('id="dailyFiguresPreview"')


def test_per_product_daily_figures_follows_daily_activity():
    assert DASHBOARD_HTML.index('id="dailyFiguresPreview"') < DASHBOARD_HTML.index('id="topProducts"')


def test_top_issued_products_follows_per_product_daily_figures():
    assert DASHBOARD_HTML.index('id="topProducts"') < DASHBOARD_HTML.index('id="byCategory"')


def test_sales_category_follows_top_products():
    assert DASHBOARD_HTML.index('id="byCategory"') < DASHBOARD_HTML.index('id="byRecipient"')


def test_recipient_follows_sales_category():
    assert DASHBOARD_HTML.index('id="byRecipient"') < DASHBOARD_HTML.index('id="attentionCard"')


def test_supporting_sections_appear_after_attention():
    assert DASHBOARD_HTML.index('id="attentionCard"') < DASHBOARD_HTML.index('id="recentDispatches"')
    assert DASHBOARD_HTML.index('id="recentDispatches"') < DASHBOARD_HTML.index('id="correctionsList"')


def test_full_required_order_holds_end_to_end():
    positions = [DASHBOARD_HTML.index(marker) for marker in SECTION_MARKERS]
    assert positions == sorted(positions)


def test_unfinalized_drafts_section_is_gone():
    assert 'id="draftList"' not in DASHBOARD_HTML
    assert 'draftRow' not in DASHBOARD_HTML
    assert 'Unfinalized drafts' not in DASHBOARD_HTML


# =====================================================================
# Three-item preview rule
# =====================================================================

def test_preview_limit_is_three():
    assert "const PREVIEW_LIMIT = 3;" in DASHBOARD_HTML


def test_preview_section_shows_all_when_three_or_fewer():
    body = re.search(r"function renderPreviewSection\(containerEl, items, rowFn, opts\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    assert "items.length > PREVIEW_LIMIT" in body  # "View all" only appears past the cap
    assert "items.slice(0, PREVIEW_LIMIT)" in body


def test_preview_section_shows_compact_empty_state_for_zero_items():
    body = re.search(r"function renderPreviewSection\(containerEl, items, rowFn, opts\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    assert "items.length === 0" in body
    assert "opts.emptyMessage" in body


def test_view_all_button_shows_item_count():
    assert '${opts.viewAllPrefix} ${items.length}</button>' in DASHBOARD_HTML


def test_grouped_issued_preview_also_caps_products_per_group():
    # Section 9/10 — a nested three-item rule: at most three groups on the
    # main Dashboard, and at most three PRODUCTS shown per group there too.
    assert "const shown = capProducts ? products.slice(0, PREVIEW_LIMIT) : products;" in DASHBOARD_HTML
    assert "group-more" in DASHBOARD_HTML  # "+N more products" hint when truncated


def test_full_modal_view_removes_the_product_per_group_cap():
    body = re.search(r"function renderGroupedIssued\(containerEl, rows, hrefFor, opts\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    assert "groupBlockHtml(r, hrefFor, false)" in body  # capProducts=false inside the modal


# =====================================================================
# One reusable modal/drawer — not seven separate implementations
# =====================================================================

def test_exactly_one_modal_element_exists():
    assert DASHBOARD_HTML.count('id="viewAllModal"') == 1


def test_modal_has_title_and_close_button():
    assert 'id="modalTitle"' in DASHBOARD_HTML
    assert 'id="modalCloseBtn"' in DASHBOARD_HTML
    assert 'aria-label="Close"' in DASHBOARD_HTML


def test_modal_supports_escape_key_close():
    assert "e.key === 'Escape'" in DASHBOARD_HTML
    assert "closeModal();" in DASHBOARD_HTML


def test_modal_supports_click_outside_close():
    assert "if(e.target.id === 'viewAllModal') closeModal();" in DASHBOARD_HTML


def test_modal_locks_background_scroll():
    assert "document.body.classList.add('modal-open');" in DASHBOARD_HTML
    assert "body.modal-open{ overflow:hidden; }" in DASHBOARD_HTML


def test_modal_manages_focus_on_open_and_close():
    assert "modalPreviouslyFocused = document.activeElement;" in DASHBOARD_HTML
    assert "document.getElementById('modalCloseBtn').focus();" in DASHBOARD_HTML
    assert "modalPreviouslyFocused.focus();" in DASHBOARD_HTML


def test_modal_is_accessible_dialog():
    assert 'role="dialog"' in DASHBOARD_HTML
    assert 'aria-modal="true"' in DASHBOARD_HTML
    assert 'aria-labelledby="modalTitle"' in DASHBOARD_HTML


def test_multiple_sections_reuse_the_same_open_modal_function():
    # Top Products, Recent Dispatches, and Corrections all go
    # through the one shared renderPreviewSection() helper — and
    # Category/Recipient both go through the one shared
    # renderGroupedIssued() helper — each of which calls openModal() in
    # exactly one place. Per-Product Daily Figures (targeted UX round) has
    # its own dedicated renderDailyFiguresToday(), since its rows must be
    # wrapped in a real <table> shell (horizontal-scroll fix) rather than
    # joined into a plain <div> — but it still follows the exact same
    # "preview N + View all" shape and calls the SAME openModal(), so this
    # remains "never N separate modal implementations", just one renderer
    # short of fully generic reuse for the one section with a genuinely
    # different markup shape.
    assert DASHBOARD_HTML.count("openModal(") >= 3  # definition + the shared renderers' call sites

    render_preview_call_sites = len(re.findall(r"renderPreviewSection\(document\.getElementById", DASHBOARD_HTML))
    assert render_preview_call_sites >= 3  # topProducts, recentDispatches, correctionsList (draftList removed)
    assert "function renderDailyFiguresToday(" in DASHBOARD_HTML
    assert "openModal('Per-product Daily Figures'" in DASHBOARD_HTML

    render_grouped_call_sites = len(re.findall(r"renderGroupedIssued\(document\.getElementById", DASHBOARD_HTML))
    assert render_grouped_call_sites == 2  # byCategory, byRecipient


# =====================================================================
# Attention — collapsed by default, critical auto-expand
# =====================================================================

def test_attention_collapsed_by_default_in_markup():
    assert '<div id="attentionList" class="collapsible hidden">' in DASHBOARD_HTML


def test_attention_header_shows_count_and_toggle():
    assert 'id="attentionCount"' in DASHBOARD_HTML
    assert 'id="attentionToggle"' in DASHBOARD_HTML
    assert 'aria-expanded="false"' in DASHBOARD_HTML


def test_attention_auto_expands_only_for_negative_closing_stock():
    body = re.search(r"function renderAttention\(attention\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    assert "n.type === 'negative_closing_stock'" in body
    assert "setAttentionExpanded(hasCritical);" in body


def test_attention_toggle_button_flips_expanded_state():
    assert "setAttentionExpanded(!expanded);" in DASHBOARD_HTML


def test_attention_calculation_logic_itself_is_unchanged():
    # This presentation correction must never touch _attention_notices()'s
    # own rule — only how the (unchanged) result is displayed.
    from webapp.services.dashboard_service import _attention_notices
    import inspect
    src = inspect.getsource(_attention_notices)
    for expected in (
        'activity["dispatch"]["finalized"] == 0',
        'activity["returns"]["finalized"] == 0',
        'row["closing_base_qty"] < 0',
    ):
        assert expected in src


# =====================================================================
# Role behavior
# =====================================================================

def test_viewer_gets_no_restricted_quick_actions():
    body = re.search(r"function renderQuickActions\(role\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    # Every gated action is behind an explicit role check — a Viewer
    # (role === 'viewer') falls through none of them.
    assert "role === 'manager' || role === 'super_admin'" in body
    assert "role === 'super_admin'" in body


def test_manager_gets_operations_but_not_reset_or_admin():
    body = re.search(r"function renderQuickActions\(role\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)
    operations_gate = re.search(r"if\(role === 'manager' \|\| role === 'super_admin'\)\{\s*actions\.push\(\{href:'/dispatch\.html'", body)
    assert operations_gate, "Open Operations must remain Manager/Super Administrator"
    # Targeted fix: Reset Daily Values is Super Administrator ONLY on the
    # Dashboard Quick Actions too — this was previously (wrongly) gated
    # the same as Open Operations, letting Manager see it here even
    # though the top nav/API/page guard were already Super-Admin-only.
    reset_gate = re.search(r"if\(role === 'super_admin'\)\{\s*actions\.push\(\{href:'/reset-daily-values\.html'", body)
    assert reset_gate, "Reset Daily Values must be gated to Super Administrator only"
    assert "if(role === 'manager' || role === 'super_admin'){\n    actions.push({href:'/reset-daily-values.html'" not in body
    admin_gate = re.search(r"if\(role === 'super_admin'\)\{\s*actions\.push\(\{href:'/admin\.html'", body)
    assert admin_gate, "Admin must remain Super Administrator only"


def test_backend_dashboard_route_still_requires_manager_or_super_admin_or_viewer():
    # Backend authorization is untouched by this presentation correction —
    # confirmed at the route-decorator level, not re-derived here. Accountant
    # was added alongside Viewer (same read-only Dashboard access) when the
    # Accountant role was introduced; Operator remains excluded.
    import inspect
    from webapp.routes import dashboard as dashboard_route
    src = inspect.getsource(dashboard_route)
    assert "roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_VIEWER)" in src


# =====================================================================
# Existing links and drill-down destinations preserved
# =====================================================================

def test_existing_drill_down_links_preserved():
    assert '/dispatch.html?open=' in DASHBOARD_HTML
    assert '/dispatch.html?sales_category_id=' in DASHBOARD_HTML
    assert '/dispatch.html?customer_id=' in DASHBOARD_HTML
    assert '/?tab=history&product=' in DASHBOARD_HTML


def test_shared_quantity_formatter_still_used_not_a_local_copy():
    assert '<script src="/quantity_format.js"></script>' in DASHBOARD_HTML
    assert "function qtyLabel(part, rule){" not in DASHBOARD_HTML  # comes from the shared file, not redefined here
