"""
Targeted fix: the Operator Daily Figures table (static/index.html's
.op-table) gets the same two corrections as the Dashboard's Per-Product
Daily Figures table (static/dashboard.html's .dfig-table — see tests/
test_dashboard_dfig_view_all_layout.py):

  1. Compact/content-aware Product column — min-width is the floor, a new
     max-width is the ceiling one unusually long product name can no
     longer stretch the whole column (and its per-row white space) past.
  2. Sticky header row + sticky Product column, so both stay visible
     while scrolling — mirrors the Dashboard table's own fix exactly:
     .op-table-wrap gets a bounded max-height + explicit overflow-y:auto
     (without it, sticky positioning has no real scrolling ancestor to
     stick against — see the CSS comment in both files for the full
     mechanism), and th/td:first-child get position:sticky; left:0 with
     opaque per-row backgrounds and correct z-index layering.

Source-level regression guards only (no JS/browser test runner exists in
this project). tests/test_final_operator_table_layout_ordering_
investigation.py's pre-existing assertions (min-width floor, no fixed-%
layout, right-aligned numeric columns, single horizontal scroll
container) all still pass unmodified — this file only adds coverage for
what's NEW.
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


def _block(marker, html=INDEX_HTML):
    idx = html.index(marker)
    return html[idx:html.index("}", idx)]


# =====================================================================
# ITEM 1 — compact / content-aware Product column
# =====================================================================

def test_operator_table_product_column_no_longer_unbounded_width():
    th_block = _block("table.op-table th:first-child{")
    assert "max-width:" in th_block
    m_min = re.search(r"min-width:\s*(\d+)px", th_block)
    m_max = re.search(r"max-width:\s*(\d+)px", th_block)
    assert m_min and m_max
    assert int(m_max.group(1)) > int(m_min.group(1))  # a real ceiling above the floor, not equal/degenerate


def test_operator_table_product_names_still_readable_and_may_wrap():
    td_block = _block("table.op-table td:first-child{")
    assert "white-space:normal" in td_block
    assert "word-break:break-word" in td_block
    # Never truncated/hidden — no ellipsis or overflow:hidden trick.
    assert "text-overflow" not in td_block
    assert "overflow:hidden" not in td_block


def test_operator_table_product_column_capped_consistently_on_header_and_body():
    th_block = _block("table.op-table th:first-child{")
    td_block = _block("table.op-table td:first-child{")
    th_max = re.search(r"max-width:\s*(\d+)px", th_block).group(1)
    td_max = re.search(r"max-width:\s*(\d+)px", td_block).group(1)
    assert th_max == td_max  # header and body cells share one column width


def test_operator_table_remaining_columns_unaffected_by_the_cap():
    # The five numeric columns keep their existing min-widths — capping
    # Product doesn't touch them; it just stops them being squeezed by an
    # oversized Product allocation.
    block = _block("table.op-table th:not(:first-child){")
    m = re.search(r"min-width:\s*(\d+)px", block)
    assert m and int(m.group(1)) == 85


def test_operator_table_no_page_level_horizontal_overflow_introduced():
    shell_idx = INDEX_HTML.index(".shell{")
    shell_block = INDEX_HTML[shell_idx:INDEX_HTML.index("}", shell_idx)]
    assert "overflow-x" not in shell_block
    assert INDEX_HTML.count("overflow-x:auto") == 1  # still only .op-table-wrap


def test_operator_table_mobile_scroll_still_contained_and_touch_enabled():
    wrap_block = _block(".op-table-wrap{")
    assert "overflow-x:auto" in wrap_block
    assert "-webkit-overflow-scrolling:touch" in wrap_block


# =====================================================================
# ITEM 2 — sticky header
# =====================================================================

def test_operator_table_header_cells_use_sticky_positioning():
    block = _block("table.op-table th{")
    assert "position:sticky" in block
    assert "top:0" in block
    assert "background:var(--paper-dim)" in block
    assert "z-index:" in block


def test_operator_table_wrap_has_bounded_height_for_sticky_to_engage():
    block = _block(".op-table-wrap{")
    assert "max-height:" in block
    assert "overflow-y:auto" in block


# =====================================================================
# ITEM 2 — sticky Product column
# =====================================================================

def test_operator_table_first_column_header_sticky_left():
    th_block = _block("table.op-table th:first-child{")
    assert "left:0" in th_block


def test_operator_table_first_column_body_cells_sticky_left_with_opaque_background():
    td_block = _block("table.op-table td:first-child{")
    assert "position:sticky" in td_block
    assert "left:0" in td_block
    assert "background:white" in td_block


def test_operator_table_sticky_column_matches_alternating_row_background():
    assert "table.op-table tbody tr:nth-child(even) td:first-child{" in INDEX_HTML
    block = _block("table.op-table tbody tr:nth-child(even) td:first-child{")
    assert "background:var(--paper-dim)" in block


def test_operator_table_top_left_cell_dual_sticky_with_correct_z_index():
    generic_th_z = int(re.search(r"z-index:\s*(\d+)", _block("table.op-table th{")).group(1))
    first_th_z = int(re.search(r"z-index:\s*(\d+)", _block("table.op-table th:first-child{")).group(1))
    first_td_z = int(re.search(r"z-index:\s*(\d+)", _block("table.op-table td:first-child{")).group(1))
    assert first_th_z > generic_th_z
    assert first_th_z > first_td_z
    # th:first-child inherits position:sticky/top:0 from the base th rule
    # (dual-sticky top+left) rather than redeclaring it.
    assert "position:sticky" not in _block("table.op-table th:first-child{")
    assert "position:sticky" in _block("table.op-table th{")


# =====================================================================
# Behavioral: desktop/tablet/mobile readability + backend/role parity
# =====================================================================

def test_operator_table_html_structure_unchanged_six_columns():
    assert (
        "<thead><tr><th>Product</th><th>Opening Stock</th><th>Production</th>"
        "<th>Returns</th><th>Issued</th><th>Closing Stock</th></tr></thead>"
    ) in INDEX_HTML


def test_operator_table_values_still_come_straight_from_backend_no_recalculation():
    idx = INDEX_HTML.index("function _operatorTableHtml(products){")
    end = INDEX_HTML.index("\n}", idx)
    body = INDEX_HTML[idx:end]
    assert "qtyLabel(r.opening, r.packaging_rule)" in body
    assert "qtyLabel(r.production, r.packaging_rule)" in body
    assert "qtyLabel(r.return_, r.packaging_rule)" in body
    assert "qtyLabel(r.issued, r.packaging_rule)" in body
    assert "qtyLabel(r.closing, r.packaging_rule)" in body


def test_operator_daily_figures_reachable_by_operator_role(client, login_as):
    login_as("op_table_layout_operator", "password123", "operator")
    res = client.get("/")
    assert res.status_code == 200


def test_dashboard_table_layout_not_regressed_by_this_operator_table_change():
    # Sanity: touching index.html's .op-table CSS must not have leaked a
    # real dfig-table CSS RULE in (a prose mention inside index.html's own
    # explanatory comment, pointing at dashboard.html by name, is fine and
    # expected — only an actual selector would be a real leak).
    assert "op-table" not in DASHBOARD_HTML
    assert "table.dfig-table" not in INDEX_HTML
    assert ".dfig-table-wrap{" not in INDEX_HTML
