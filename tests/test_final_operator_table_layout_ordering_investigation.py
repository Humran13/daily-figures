"""
Operator Daily Figures — show table before activity + fix horizontal
scrolling.

Two changes, confirmed with the user before implementation (the field
names "Received"/"Crates Sold"/"Damages"/"Personal Use" mentioned in the
originating request don't exist in this app — Production/Returns/Issued
are this app's actual activity fields, used throughout):

1. Backend (webapp/routes/daily_figures.py::operator_summary()): every
   active product is now ALWAYS listed — never filtered out for having
   no activity — sorted so products with real activity for this exact
   Date + Shift come first (in their existing product_usage_service
   ranking order), followed by untouched products (same underlying
   order). Opening Stock/Closing Stock alone never counts as "activity".
   Every field is the real daily_figure_view() value for every row — no
   placeholder nulling. This replaces the two-round-old "preview mode
   filters everyone out until any activity exists" design.

2. CSS (static/index.html): the previous `min-width:520px` on the table
   combined with `white-space:nowrap` on every header forced the table to
   be at least as wide as this app's entire (deliberately narrow,
   single-column) content area, guaranteeing horizontal scroll on every
   normal viewport. Replaced with `table-layout:fixed` and explicit
   proportional column widths (Product wider than the five numeric
   columns), smaller compact fonts/padding, and header text allowed to
   wrap onto two lines.
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


def test_table_min_width_no_longer_forces_horizontal_scroll():
    css = _table_css_block()
    assert "min-width:520px" not in css
    assert "min-width: 520px" not in css


def test_table_uses_fixed_layout_with_proportional_columns():
    css = _table_css_block()
    assert "table-layout:fixed" in css
    assert "width:100%" in css or "width: 100%" in css


def test_product_column_wider_than_numeric_columns():
    css = _table_css_block()
    first_idx = css.index("th:first-child")
    first_block = css[first_idx:css.index(";", css.index("width", first_idx)) + 1]
    other_idx = css.index("th:not(:first-child)")
    other_block = css[other_idx:css.index(";", css.index("width", other_idx)) + 1]
    import re
    first_width = float(re.search(r"width:\s*([\d.]+)%", first_block).group(1))
    other_width = float(re.search(r"width:\s*([\d.]+)%", other_block).group(1))
    assert first_width > other_width


def test_table_headers_allowed_to_wrap():
    css = _table_css_block()
    th_idx = css.index("table.op-table th{")
    th_block = css[th_idx:css.index("}", th_idx)]
    assert "white-space:normal" in th_block or "white-space: normal" in th_block


def test_table_wrapper_still_has_a_safety_net_scroll_container():
    """A last-resort horizontal scroll on the table's OWN wrapper (never
    the whole page) remains, in case of an unusually narrow viewport —
    the fix is that it should never be NEEDED on a normal viewport, not
    that the safety net itself was removed."""
    assert "overflow-x:auto" in INDEX_HTML


def test_no_new_cards_buttons_or_summaries_introduced():
    idx = INDEX_HTML.index("async function renderOperatorTable(){")
    end = INDEX_HTML.index("\nfunction _operatorTableHtml", idx)
    body = INDEX_HTML[idx:end]
    assert "<button" not in body
    assert "<input" not in body


# =====================================================================
# PRODUCT VISIBILITY AND ORDERING
# =====================================================================

def test_every_product_is_listed_including_untouched_ones(client, super_admin):
    untouched = _make_product(client, "Untouched Product")
    worked = _make_product(client, "Worked Product")
    _finalize_production(client, worked["id"], "2026-08-01", "Day", 2)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    assert untouched["id"] in ids
    assert worked["id"] in ids


def test_worked_on_products_appear_before_untouched_products(client, super_admin):
    untouched1 = _make_product(client, "Untouched A")
    worked = _make_product(client, "Worked A")
    untouched2 = _make_product(client, "Untouched B")
    _finalize_production(client, worked["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    worked_index = ids.index(worked["id"])
    assert worked_index < ids.index(untouched1["id"])
    assert worked_index < ids.index(untouched2["id"])


def test_opening_stock_alone_does_not_count_as_worked_on(client, super_admin):
    p = _make_product(client, "Opening Only Product")
    trigger = _make_product(client, "Ordering Trigger Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 999, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, trigger["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    assert ids.index(trigger["id"]) < ids.index(p["id"])


def test_closing_stock_alone_does_not_count_as_worked_on(client, super_admin):
    p = _make_product(client, "Closing Only Product")
    trigger = _make_product(client, "Ordering Trigger Product 2")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, trigger["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")  # a later, untouched period — Closing is just carried-forward Opening
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["closing"]["base_qty"] == 5000  # a real, non-zero Closing...
    ids = [r["product_id"] for r in data["products"]]
    assert ids.index(trigger["id"]) < ids.index(p["id"])  # ...yet still ordered after the genuinely worked-on product


def test_multiple_worked_on_products_all_precede_all_untouched_products(client, super_admin):
    untouched = [_make_product(client, f"Untouched Multi {i}") for i in range(2)]
    worked = [_make_product(client, f"Worked Multi {i}") for i in range(2)]
    for w in worked:
        _finalize_production(client, w["id"], "2026-08-01", "Day", 1)

    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    max_worked_index = max(ids.index(w["id"]) for w in worked)
    min_untouched_index = min(ids.index(u["id"]) for u in untouched)
    assert max_worked_index < min_untouched_index


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
