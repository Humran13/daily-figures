"""
Targeted frontend fix: the Per-Product Daily Figures "View all" modal
(static/dashboard.html) was forcing horizontal panning even on desktop/
tablet, clipping the Closing Stock header, and losing both the header row
and the Product column while scrolling.

Root cause (see the completion report for the full writeup): the shared
"View all" modal (.modal-panel) was hard-capped at max-width:640px on
every viewport, while table.dfig-table's own min-width (now 650px) barely
fit inside it — so the table always needed to scroll, even on a 1440px
screen. Separately, .dfig-table-wrap had no bounded height, so its sticky
thead never had a real scrolling ancestor to stick against — the outer
.modal-body scrolled instead, and the sticky header did nothing.

Fix: an opt-in `.modal-panel--wide` class (only applied via openModal()'s
new `{wide:true}` option, only passed by the Daily Figures "View all"
call site) lets the panel grow up to 960px — width:100% keeps this inert
on any viewport narrower than that, so no separate media query is
needed and every other "View all" modal (Top issued products, Recent
dispatches/corrections, Issued by category/recipient) is completely
unaffected. .dfig-table-wrap got a bounded max-height so its own
overflow-x/overflow-y:auto becomes a genuine scroll container, which is
what makes the (now per-th, more robust) sticky header actually stick.
The first column (th and td) is now also position:sticky; left:0, with
an explicit opaque background per row, so Product stays visible while
panning horizontally on narrow screens.

Source-level regression guards only (no JS/browser test runner exists in
this project — see every other frontend-only stage's tests, e.g.
tests/test_final_dashboard_ux_layout.py, for the same pattern).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _block(marker, html=DASHBOARD_HTML):
    idx = html.index(marker)
    return html[idx:html.index("}", idx)]


# =====================================================================
# 1 — all six columns still present (View all reuses the exact same
#     shell/markup as the preview — see _dailyFiguresTableShell())
# =====================================================================

def test_view_all_table_keeps_all_six_required_columns():
    assert (
        "<th>Product</th><th>Opening Stock</th><th>Production</th>"
        "<th>Returns</th><th>Issued</th><th>Closing Stock</th>"
    ) in DASHBOARD_HTML


def test_view_all_and_preview_share_the_same_table_shell():
    # One function builds the table for BOTH the compact preview and the
    # full "View all" modal — proves there is no second, diverging markup
    # to keep in sync.
    idx = DASHBOARD_HTML.index("function renderDailyFiguresToday(")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    assert body.count("_dailyFiguresTableShell(") == 2


# =====================================================================
# 2 — Closing Stock is not clipped/hidden; headers may wrap instead of
#     being forced into overflow
# =====================================================================

def test_closing_stock_header_not_hidden_or_truncated():
    assert "Closing Stock</th>" in DASHBOARD_HTML
    assert "Closing Stoc</th>" not in DASHBOARD_HTML  # no truncated label


def test_dfig_headers_allowed_to_wrap_not_forced_nowrap():
    block = _block("table.dfig-table th{")
    assert "white-space:normal" in block
    assert "white-space:nowrap" not in block


def test_dfig_table_does_not_clip_header_text():
    # Nothing in the .dfig-table rule family may hide/clip content merely
    # to make columns fit — text-overflow/hidden tricks are explicitly
    # disallowed by the spec for this table.
    idx = DASHBOARD_HTML.index("table.dfig-table th{")
    end = DASHBOARD_HTML.index("\n  .figure-name{", idx)
    section = DASHBOARD_HTML[idx:end]
    assert "text-overflow" not in section
    assert "overflow:hidden" not in section


# =====================================================================
# 3 — desktop: no oversized mobile min-width forced on wide screens; the
#     modal itself must be allowed to grow, not just the table
# =====================================================================

def test_dfig_table_min_width_is_reasonable_not_a_giant_mobile_value():
    block = _block("table.dfig-table{")
    m = re.search(r"min-width:\s*(\d+)px", block)
    assert m
    width = int(m.group(1))
    # Large enough to guarantee every column has breathing room, but not
    # an arbitrarily huge "mobile-first" number that would force
    # scrolling even in a reasonably-sized desktop container.
    assert 500 <= width <= 800


def test_view_all_modal_can_grow_wider_than_the_default_modal_cap():
    default_block = _block(".modal-panel{")
    default_max = int(re.search(r"max-width:\s*(\d+)px", default_block).group(1))
    wide_block = _block(".modal-panel.modal-panel--wide{")
    wide_max = int(re.search(r"max-width:\s*(\d+)px", wide_block).group(1))
    assert wide_max > default_max
    # Wide enough that the table's own min-width comfortably fits with
    # room to spare (padding, borders) — i.e. no horizontal scroll forced
    # on any screen at least this wide.
    table_min_width = int(re.search(r"min-width:\s*(\d+)px", _block("table.dfig-table{")).group(1))
    assert wide_max - table_min_width >= 200


def test_wide_modal_class_only_applied_to_daily_figures_view_all():
    # openModal() takes an opt-in third argument — every OTHER "View all"
    # call site in this file must still be a plain two-argument call, so
    # Top issued products / Recent dispatches & corrections / Issued by
    # category & recipient are unaffected.
    assert "openModal('Per-product Daily Figures', _dailyFiguresTableShell(items.map(figureRow).join('')), {wide: true});" in DASHBOARD_HTML
    assert "openModal(opts.modalTitle, items.map(rowFn).join(''));" in DASHBOARD_HTML
    assert "openModal(opts.modalTitle, rows.map(r => groupBlockHtml(r, hrefFor, false)).join(''));" in DASHBOARD_HTML


def test_wide_modal_class_toggled_off_when_not_requested():
    idx = DASHBOARD_HTML.index("function openModal(")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    # Every other caller omits `opts` — the toggle must default to
    # removing the wide class, never leaving a stale one from a previous
    # Daily Figures modal open.
    assert "classList.toggle('modal-panel--wide', !!(opts && opts.wide))" in body


# =====================================================================
# 4 — no unnecessary horizontal scrollbar on desktop/tablet; small mobile
#     still scrolls (contained to the table wrapper)
# =====================================================================

def test_dfig_table_wrap_still_scrolls_horizontally_for_narrow_screens():
    block = _block(".dfig-table-wrap{")
    assert "overflow-x:auto" in block


def test_dfig_table_wrap_scroll_is_contained_not_page_level():
    shell_block = _block(".shell{")
    assert "overflow-x" not in shell_block
    body_block = _block("body{")
    assert "overflow-x" not in body_block


# =====================================================================
# 5 — sticky table header
# =====================================================================

def test_dfig_header_cells_use_sticky_positioning():
    block = _block("table.dfig-table th{")
    assert "position:sticky" in block
    assert "top:0" in block
    assert "background:var(--paper-dim)" in block  # opaque — rows must not show through
    assert "z-index:" in block


def test_dfig_table_wrap_has_bounded_height_so_sticky_actually_engages():
    # Sticky positions relative to the nearest scrolling ancestor — a
    # wrapper with unbounded height never scrolls itself, so its sticky
    # descendants never visibly stick. max-height is what turns this
    # wrapper into that real scrolling ancestor.
    block = _block(".dfig-table-wrap{")
    assert "max-height:" in block
    assert "overflow-y:auto" in block


# =====================================================================
# 6 — sticky Product column
# =====================================================================

def test_dfig_first_column_header_and_body_cells_sticky_left():
    # th:first-child inherits position:sticky from the base `th` rule
    # (dual-sticky top+left — see test_dfig_top_left_header_inherits_
    # sticky_top_from_base_th_rule below) and only adds left:0 here.
    generic_th_block = _block("table.dfig-table th{")
    assert "position:sticky" in generic_th_block
    th_block = _block("table.dfig-table th:first-child{")
    assert "left:0" in th_block

    td_block = _block("table.dfig-table td:first-child{")
    assert "position:sticky" in td_block
    assert "left:0" in td_block
    assert "background:white" in td_block  # opaque — scrolling columns must not bleed through


def test_dfig_sticky_first_column_has_matching_background_on_alternating_rows():
    assert "table.dfig-table tbody tr:nth-child(even) td:first-child{" in DASHBOARD_HTML
    block = _block("table.dfig-table tbody tr:nth-child(even) td:first-child{")
    assert "background:var(--paper-dim)" in block


# =====================================================================
# 6b — top-left Product header cell: combined sticky top + left, correct
#      z-index layering above both the ordinary sticky header row and the
#      sticky first body column
# =====================================================================

def test_dfig_top_left_header_cell_has_higher_z_index_than_body_column():
    th_block = _block("table.dfig-table th:first-child{")
    th_z = int(re.search(r"z-index:\s*(\d+)", th_block).group(1))

    generic_th_block = _block("table.dfig-table th{")
    generic_th_z = int(re.search(r"z-index:\s*(\d+)", generic_th_block).group(1))

    td_block = _block("table.dfig-table td:first-child{")
    td_z = int(re.search(r"z-index:\s*(\d+)", td_block).group(1))

    assert th_z > generic_th_z
    assert th_z > td_z


def test_dfig_top_left_header_inherits_sticky_top_from_base_th_rule():
    # th:first-child only adds left:0/z-index — position:sticky and top:0
    # come from the base `table.dfig-table th` rule it's layered on top
    # of, so it's genuinely dual-sticky (top AND left), not left-only.
    base_th_block = _block("table.dfig-table th{")
    assert "position:sticky" in base_th_block
    assert "top:0" in base_th_block
    first_th_block = _block("table.dfig-table th:first-child{")
    assert "position:sticky" not in first_th_block  # not redundantly re-declared
    assert "left:0" in first_th_block


# =====================================================================
# 7 — mobile horizontal scrolling stays contained to the table wrapper
# =====================================================================

def test_mobile_scroll_touch_support_present():
    block = _block(".dfig-table-wrap{")
    assert "-webkit-overflow-scrolling:touch" in block


# =====================================================================
# 8 — page/body never gains horizontal overflow
# =====================================================================

def test_no_horizontal_overflow_on_shell_or_body():
    shell_block = _block(".shell{")
    assert "overflow-x" not in shell_block
    body_block = _block("body{")
    assert "overflow-x" not in body_block
    assert "overflow-x" not in _block(".modal-overlay{")
    assert "overflow-x" not in _block(".modal-panel{")


# =====================================================================
# 9 — default (non-View-All) preview is not regressed
# =====================================================================

def test_preview_still_renders_via_same_shell_function():
    idx = DASHBOARD_HTML.index("function renderDailyFiguresToday(")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    assert "const preview = items.slice(0, PREVIEW_LIMIT);" in body
    assert "_dailyFiguresTableShell(preview.map(figureRow).join(''))" in body


def test_preview_view_all_button_and_threshold_unchanged():
    assert "PREVIEW_LIMIT = 3" in DASHBOARD_HTML
    assert "if(items.length > PREVIEW_LIMIT){" in DASHBOARD_HTML
    assert 'View all ${items.length}' in DASHBOARD_HTML


def test_dfig_values_still_come_straight_from_figure_row_no_recalculation():
    idx = DASHBOARD_HTML.index("function figureRow(")
    end = DASHBOARD_HTML.index("\nfunction _dailyFiguresTableShell", idx)
    body = DASHBOARD_HTML[idx:end]
    assert "qtyLabel(r.opening, r.packaging_rule)" in body
    assert "qtyLabel(r.production, r.packaging_rule)" in body
    assert "qtyLabel(r.return_, r.packaging_rule)" in body
    assert "qtyLabel(r.issued, r.packaging_rule)" in body
    assert "qtyLabel(r.closing, r.packaging_rule)" in body


# =====================================================================
# 10 — no role-specific table behavior introduced
# =====================================================================

def test_dfig_table_css_and_markup_have_no_role_conditionals():
    idx = DASHBOARD_HTML.index("function _dailyFiguresTableShell(")
    end = DASHBOARD_HTML.index("\n}", idx)
    shell_fn = DASHBOARD_HTML[idx:end]
    assert "role" not in shell_fn

    idx2 = DASHBOARD_HTML.index("function renderDailyFiguresToday(")
    end2 = DASHBOARD_HTML.index("\n}", idx2)
    render_fn = DASHBOARD_HTML[idx2:end2]
    assert "role" not in render_fn

    # The CSS block covering .dfig-table itself contains no role selectors.
    css_idx = DASHBOARD_HTML.index("table.dfig-table th{")
    css_end = DASHBOARD_HTML.index("\n  .figure-name{", css_idx)
    assert "role" not in DASHBOARD_HTML[css_idx:css_end]


def test_dashboard_page_reachable_by_every_role_that_should_see_it(client, login_as):
    for role in ("super_admin", "manager", "accountant", "viewer"):
        login_as(f"dfig_layout_{role}", "password123", role)
        res = client.get("/dashboard.html")
        assert res.status_code == 200, f"{role} should reach dashboard.html unchanged by this layout fix"
        client.post("/api/logout")


def test_operator_dashboard_access_unchanged_by_this_layout_fix(client, login_as):
    # Operator never had Dashboard access — this presentation-only fix
    # must not change that (nothing here touches backend authorization).
    login_as("dfig_layout_operator", "password123", "operator")
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/dashboard.html"


# =====================================================================
# Unrelated area sanity: index.html's separate Operator Daily Figures
# table (.op-table) is a different component and must be untouched.
# =====================================================================

def test_operator_table_in_index_html_is_untouched_by_this_fix():
    assert "modal-panel--wide" not in INDEX_HTML
    assert "Per-product Daily Figures" not in INDEX_HTML


# =====================================================================
# Follow-up targeted fix: compact / content-aware Product column (see
# tests/test_operator_table_sticky_and_compact_layout.py for the
# Operator table's identical companion fix).
# =====================================================================

def test_dfig_product_column_no_longer_unbounded_width():
    th_block = _block("table.dfig-table th:first-child{")
    assert "max-width:" in th_block
    m_min = re.search(r"min-width:\s*(\d+)px", th_block)
    m_max = re.search(r"max-width:\s*(\d+)px", th_block)
    assert m_min and m_max
    assert int(m_max.group(1)) > int(m_min.group(1))


def test_dfig_product_column_capped_consistently_on_header_and_body():
    th_max = re.search(r"max-width:\s*(\d+)px", _block("table.dfig-table th:first-child{")).group(1)
    td_max = re.search(r"max-width:\s*(\d+)px", _block("table.dfig-table td:first-child{")).group(1)
    assert th_max == td_max


def test_dfig_product_names_never_truncated_or_hidden():
    td_block = _block("table.dfig-table td:first-child{")
    assert "white-space:normal" in td_block
    assert "word-break:break-word" in td_block
    assert "text-overflow" not in td_block
    assert "overflow:hidden" not in td_block
