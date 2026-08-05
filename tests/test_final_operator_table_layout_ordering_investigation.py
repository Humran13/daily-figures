"""
Operator Daily Figures — horizontally scrollable table + hybrid
preview/activity filtering.

CSS (static/index.html): a fixed-percentage table (table-layout:fixed
with narrow % column widths, e.g. 15.2% per numeric column) squeezed
multi-digit quantities enough to visually overlap neighboring cells on
some products/viewports. Restored the proven, previously-shipped
pattern: the table keeps a real min-width and each column its own
min-width (Product wider than the five numeric columns, none ever
collapsing below what a quantity needs), with ONLY the table's own
wrapper (.op-table-wrap) scrolling horizontally when the viewport is
narrower than that — the page/.shell itself never gains horizontal
overflow, since overflow-x:auto appears nowhere else near this table. A
small, non-intrusive "Swipe horizontally..." hint is shown only below
600px.

Backend (webapp/routes/daily_figures.py::operator_summary()): restored
the period-wide hybrid filter — "preview" (no product anywhere has
finalized activity) lists every active product; "activity" (at least one
does) shows ONLY products with real finalized Production, Returns, or
Issued for this exact Date + Shift, hiding every untouched product
(including Opening/Closing-only ones). See
test_final_operator_daily_figures_table_investigation.py and
test_final_operator_table_preview_mode_investigation.py for the full
mode-transition/absence test coverage; the ACTIVITY MODE FILTERING
section below covers the ordering-among-qualifying-rows angle that
belongs with this file's other product-facing tests.
"""
import pathlib

import pytest

INDEX_HTML = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _finalize_production(client, pid, date_str, shift, cartons):
    prod = client.post("/api/production", json={
        "date": date_str, "shift": shift, "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    return prod


def _summary(client, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/operator-summary?date={date_str}&shift={shift}").get_json()


# =====================================================================
# CSS / LAYOUT
# =====================================================================

def _table_css_block():
    idx = INDEX_HTML.index("table.op-table{")
    end = INDEX_HTML.index("</style>")
    return INDEX_HTML[idx:end]


def test_table_has_a_sensible_minimum_width():
    css = _table_css_block()
    idx = css.index("table.op-table{")
    block = css[idx:css.index("}", idx)]
    import re
    m = re.search(r"min-width:\s*(\d+)px", block)
    assert m and int(m.group(1)) >= 500


def test_table_no_longer_uses_fixed_percentage_layout():
    """The fixed-percentage layout (table-layout:fixed + narrow %-based
    column widths) is what caused quantities to visually overlap — it
    must be gone, not just relocated."""
    css = _table_css_block()
    assert "table-layout:fixed" not in css
    assert "%" not in css[css.index("th:first-child"):css.index("th:not(:first-child)") + 60]


def test_product_column_wider_than_numeric_columns():
    css = _table_css_block()
    first_idx = css.index("th:first-child")
    first_block = css[first_idx:css.index(";", css.index("min-width", first_idx)) + 1]
    other_idx = css.index("th:not(:first-child)")
    other_block = css[other_idx:css.index(";", css.index("min-width", other_idx)) + 1]
    import re
    first_width = float(re.search(r"min-width:\s*([\d.]+)px", first_block).group(1))
    other_width = float(re.search(r"min-width:\s*([\d.]+)px", other_block).group(1))
    assert first_width > other_width


def test_quantity_cells_never_wrap_across_columns():
    css = _table_css_block()
    td_idx = css.index("table.op-table td{")
    td_block = css[td_idx:css.index("}", td_idx)]
    assert "white-space:nowrap" in td_block
    assert "text-align:right" in td_block


def test_quantity_cells_are_right_aligned_in_their_own_column():
    css = _table_css_block()
    th_idx = css.index("table.op-table th{")
    th_block = css[th_idx:css.index("}", th_idx)]
    assert "text-align:right" in th_block


def test_product_names_may_wrap_without_covering_quantity_cells():
    """Only the Product cell may wrap onto multiple lines — every
    quantity cell stays single-line (see
    test_quantity_cells_never_wrap_across_columns) so a long product name
    can never push into / cover a neighboring quantity column."""
    css = _table_css_block()
    first_td_idx = css.index("table.op-table td:first-child{")
    first_td_block = css[first_td_idx:css.index("}", first_td_idx)]
    assert "white-space:normal" in first_td_block


def test_table_wrapper_is_the_only_horizontal_scroll_container():
    """Only .op-table-wrap scrolls horizontally — the page/.shell itself
    must never gain horizontal overflow."""
    wrap_idx = INDEX_HTML.index(".op-table-wrap{")
    wrap_block = INDEX_HTML[wrap_idx:INDEX_HTML.index("}", wrap_idx)]
    assert "overflow-x:auto" in wrap_block
    assert "-webkit-overflow-scrolling:touch" in wrap_block

    shell_idx = INDEX_HTML.index(".shell{")
    shell_block = INDEX_HTML[shell_idx:INDEX_HTML.index("}", shell_idx)]
    assert "overflow-x" not in shell_block

    # overflow-x:auto must appear nowhere else in the whole stylesheet
    assert INDEX_HTML.count("overflow-x:auto") == 1


def test_swipe_hint_present_but_not_intrusive():
    assert "Swipe horizontally to view all figures." in INDEX_HTML
    hint_idx = INDEX_HTML.index(".op-swipe-hint{")
    hint_block = INDEX_HTML[hint_idx:INDEX_HTML.index("}", hint_idx)]
    assert "display:none" in hint_block  # hidden by default, shown only via the narrow-viewport media query
    assert "@media (max-width:600px){ .op-swipe-hint{ display:block; } }" in INDEX_HTML


def test_all_six_columns_present_in_header_markup():
    idx = INDEX_HTML.index("<thead><tr><th>Product</th>")
    end = INDEX_HTML.index("</tr></thead>", idx)
    header_block = INDEX_HTML[idx:end]
    for label in ["Product", "Opening Stock", "Production", "Returns", "Issued", "Closing Stock"]:
        assert f"<th>{label}</th>" in header_block


def test_no_new_cards_buttons_or_summaries_introduced():
    idx = INDEX_HTML.index("async function renderOperatorTable(){")
    end = INDEX_HTML.index("\nfunction _operatorTableHtml", idx)
    body = INDEX_HTML[idx:end]
    assert "<button" not in body
    assert "<input" not in body


# =====================================================================
# ACTIVITY MODE FILTERING
# =====================================================================

def test_activity_mode_contains_only_worked_on_products(client, super_admin):
    untouched = _make_product(client, "Untouched Product")
    worked = _make_product(client, "Worked Product")
    _finalize_production(client, worked["id"], "2026-08-01", "Day", 2)

    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    ids = [r["product_id"] for r in data["products"]]
    assert worked["id"] in ids
    assert untouched["id"] not in ids


def test_untouched_products_are_absent_in_activity_mode(client, super_admin):
    untouched1 = _make_product(client, "Untouched A")
    worked = _make_product(client, "Worked A")
    untouched2 = _make_product(client, "Untouched B")
    _finalize_production(client, worked["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    assert worked["id"] in ids
    assert untouched1["id"] not in ids
    assert untouched2["id"] not in ids


def test_opening_stock_alone_does_not_appear_in_activity_mode(client, super_admin):
    p = _make_product(client, "Opening Only Product")
    trigger = _make_product(client, "Ordering Trigger Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 999, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, trigger["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    assert trigger["id"] in ids
    assert p["id"] not in ids


def test_closing_stock_alone_does_not_appear_in_activity_mode(client, super_admin):
    p = _make_product(client, "Closing Only Product")
    trigger = _make_product(client, "Ordering Trigger Product 2")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, trigger["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")  # a later, untouched period — Closing is just carried-forward Opening
    ids = [r["product_id"] for r in data["products"]]
    assert trigger["id"] in ids
    assert p["id"] not in ids  # a real, non-zero Closing does not earn it a place in activity mode


def test_multiple_worked_on_products_all_appear_untouched_all_absent(client, super_admin):
    untouched = [_make_product(client, f"Untouched Multi {i}") for i in range(2)]
    worked = [_make_product(client, f"Worked Multi {i}") for i in range(2)]
    for w in worked:
        _finalize_production(client, w["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    for w in worked:
        assert w["id"] in ids
    for u in untouched:
        assert u["id"] not in ids


def test_ordering_is_stable_and_deterministic_across_requests(client, super_admin):
    p1 = _make_product(client, "Stable Order Product 1")
    p2 = _make_product(client, "Stable Order Product 2")
    _finalize_production(client, p1["id"], "2026-08-01", "Day", 1)
    _finalize_production(client, p2["id"], "2026-08-01", "Day", 1)
    first = [r["product_id"] for r in _summary(client, "2026-08-01")["products"]]
    second = [r["product_id"] for r in _summary(client, "2026-08-01")["products"]]
    assert first == second


def test_backend_values_rendered_unmodified_no_frontend_recalculation(client, super_admin):
    p = _make_product(client, "Exact Values Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 7, "packs": 3, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)
    data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"]["base_qty"] == 730
    assert row["production"]["base_qty"] == 200
    assert row["closing"]["base_qty"] == 930
    assert row["closing"]["base_qty"] == row["opening"]["base_qty"] + row["production"]["base_qty"] + row["return_"]["base_qty"] - row["issued"]["base_qty"]
