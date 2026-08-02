"""
Final Dashboard correction — all quantity reporting must use carton-based,
packaging-aware book notation, never raw base-unit pieces, and a group
spanning several different products (Sales Category / Recipient) must
never be collapsed into one misleading combined "cartons" figure — each
product's own quantity is shown separately instead.

webapp/services/stock_service.py's recipient_totals() now returns a
`products` breakdown per group (product id/name/base_qty/packaging_rule/
quantity_label, via the one centralized qty_label() formatter), and
static/dashboard.html now loads the shared /quantity_format.js instead of
a stale local reimplementation that had drifted out of sync with the
current point-notation convention for carton+piece-only products.
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Notation Category"}).get_json()
    other_category = client.post("/api/admin/sales-categories", json={"name": "Notation Other Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Notation Customer", "sales_category_id": category["id"]}).get_json()
    other_customer = client.post("/api/admin/customers", json={"name": "Notation Other Customer", "sales_category_id": other_category["id"]}).get_json()
    return {"category": category, "other_category": other_category, "customer": customer, "other_customer": other_customer}


def _make_product(client, name, rule):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule)
    return p


def _finalize(client, product_id, customer_id, category_id, number, cartons, packs=0, pieces=0, date_str="2026-07-28"):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day",
        "customer_id": customer_id, "sales_category_id": category_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _group(rows, name):
    return next(r for r in rows if r["group_name"] == name)


def _recipient_totals(client, group_by, date_from="2026-06-29", date_to="2026-07-28"):
    res = client.get(f"/api/reports/recipient-totals?date_from={date_from}&date_to={date_to}&group_by={group_by}")
    assert res.status_code == 200, res.get_json()
    return res.get_json()


# =====================================================================
# Top Issued Products — carton/book notation, not raw pieces
# =====================================================================

def test_top_products_carries_packaging_aware_parts_not_only_raw_pieces(client, setup):
    compact = _make_product(client, "Notation Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "TP-1", cartons=109, packs=5, pieces=0)

    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    entry = next(p for p in dashboard["top_products"] if p["product_id"] == compact["id"])
    assert entry["cartons"] == 109 and entry["packs"] == 5 and entry["pieces"] == 0
    assert isinstance(entry["base_qty"], int)

    from webapp.services.quantity_format import qty_label
    assert qty_label(entry["cartons"], entry["packs"], entry["pieces"], entry["packaging_rule"]) == "109.50 Ctns"


def test_dashboard_html_uses_shared_formatter_not_a_stale_local_copy():
    assert '<script src="/quantity_format.js"></script>' in DASHBOARD_HTML
    assert "function qty(part, rule){" not in DASHBOARD_HTML  # the old, drifted local copy is gone
    assert "qtyLabel(" in DASHBOARD_HTML


# =====================================================================
# Sales Category / Recipient reporting — never raw pieces
# =====================================================================

def test_sales_category_report_never_shows_raw_pieces_text(client, setup):
    compact = _make_product(client, "Notation Category Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "SC-1", cartons=235)

    rows = _recipient_totals(client, "category")
    row = _group(rows, "Notation Category")
    assert row["products"][0]["quantity_label"] == "235 Ctns"
    assert "pieces" not in row["products"][0]["quantity_label"]


def test_recipient_report_never_shows_raw_pieces_text(client, setup):
    napkins = _make_product(client, "Notation Recipient Napkins", {"cartons_to_packs": 6, "packs_to_pieces": 10})
    _finalize(client, napkins["id"], setup["customer"]["id"], setup["category"]["id"], "REC-1", cartons=85)

    rows = _recipient_totals(client, "recipient")
    row = _group(rows, "Notation Customer")
    assert row["products"][0]["quantity_label"] == "85 Ctns"
    assert "pieces" not in row["products"][0]["quantity_label"]


def test_dashboard_html_no_longer_appends_pieces_suffix_to_group_totals():
    assert "} pieces</div>" not in DASHBOARD_HTML  # the old raw-piece rendering is gone
    assert "total_issued_base_qty} pieces" not in DASHBOARD_HTML


# =====================================================================
# Mixed-product groups — broken down by product, never one combined total
# =====================================================================

def test_mixed_product_category_totals_broken_down_by_product(client, setup):
    compact = _make_product(client, "Notation Mixed Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    napkins = _make_product(client, "Notation Mixed Napkins", {"cartons_to_packs": 6, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "MX-1", cartons=235)
    _finalize(client, napkins["id"], setup["customer"]["id"], setup["category"]["id"], "MX-2", cartons=85)

    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["dispatch_count"] == 2
    names = {p["product_name"] for p in row["products"]}
    assert names == {"Notation Mixed Compact", "Notation Mixed Napkins"}
    labels = {p["product_name"]: p["quantity_label"] for p in row["products"]}
    assert labels["Notation Mixed Compact"] == "235 Ctns"
    assert labels["Notation Mixed Napkins"] == "85 Ctns"


def test_mixed_product_recipient_totals_broken_down_by_product(client, setup):
    compact = _make_product(client, "Notation Mixed Recipient Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    kingmax = _make_product(client, "Notation Mixed Recipient KingMax", {"carton_to_pieces": 60})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "MXR-1", cartons=50)
    _finalize(client, kingmax["id"], setup["customer"]["id"], setup["category"]["id"], "MXR-2", cartons=5, pieces=3)

    row = _group(_recipient_totals(client, "recipient"), "Notation Customer")
    labels = {p["product_name"]: p["quantity_label"] for p in row["products"]}
    assert labels["Notation Mixed Recipient Compact"] == "50 Ctns"
    assert labels["Notation Mixed Recipient KingMax"] == "5.03 Ctns"


def test_different_carton_capacities_never_combined_into_one_carton_total(client, setup):
    """A group's own top-level dict must never itself carry a formatted
    carton figure derived from summing different products' cartons — only
    the raw, honestly-named total_issued_base_qty (pieces, used by exports
    and internal consumers) and the per-product breakdown."""
    compact = _make_product(client, "Notation Capacity Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    kingmax = _make_product(client, "Notation Capacity KingMax", {"carton_to_pieces": 60})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "CAP-1", cartons=10)
    _finalize(client, kingmax["id"], setup["customer"]["id"], setup["category"]["id"], "CAP-2", cartons=10)

    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert "quantity_label" not in row
    assert "cartons" not in row
    # The two products' cartons must never be silently added together —
    # each keeps its own count under its own packaging rule.
    per_product = {p["product_name"]: p["cartons"] for p in row["products"]}
    assert per_product["Notation Capacity Compact"] == 10
    assert per_product["Notation Capacity KingMax"] == 10


# =====================================================================
# Single-product groups
# =====================================================================

def test_single_product_group_displays_carton_quantity_correctly(client, setup):
    straws = _make_product(client, "Notation Single Straws", {"cartons_to_packs": 12, "packs_to_pieces": 100})
    _finalize(client, straws["id"], setup["customer"]["id"], setup["category"]["id"], "SP-1", cartons=1)

    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert len(row["products"]) == 1
    assert row["products"][0]["quantity_label"] == "1 Ctns"


# =====================================================================
# Point-notation correctness per configuration (regression, unaffected)
# =====================================================================

def test_compact_three_tier_notation_remains_correct(client, setup):
    compact = _make_product(client, "Notation Regression Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "RG-1", cartons=109, packs=5, pieces=0)
    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["products"][0]["quantity_label"] == "109.50 Ctns"


def test_napkin_mixed_radix_notation_remains_correct(client, setup):
    napkins = _make_product(client, "Notation Regression Napkins", {"cartons_to_packs": 6, "packs_to_pieces": 10})
    _finalize(client, napkins["id"], setup["customer"]["id"], setup["category"]["id"], "RG-2", cartons=135, packs=4, pieces=0)
    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["products"][0]["quantity_label"] == "135.40 Ctns"


def test_kingmax_carton_plus_piece_notation_remains_correct(client, setup):
    kingmax = _make_product(client, "Notation Regression KingMax", {"carton_to_pieces": 60})
    _finalize(client, kingmax["id"], setup["customer"]["id"], setup["category"]["id"], "RG-3", cartons=5, pieces=3)
    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["products"][0]["quantity_label"] == "5.03 Ctns"


def test_jumbomax_carton_plus_piece_normalization_remains_correct(client, setup):
    jumbomax = _make_product(client, "Notation Regression JumboMax", {"carton_to_pieces": 24})
    _finalize(client, jumbomax["id"], setup["customer"]["id"], setup["category"]["id"], "RG-4", cartons=4, pieces=12)
    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["products"][0]["quantity_label"] == "4.12 Ctns"
    assert row["products"][0]["cartons"] == 4
    assert row["products"][0]["pieces"] == 12


# =====================================================================
# Zero-quantity lines omitted from grouped breakdowns
# =====================================================================

def test_zero_quantity_product_lines_omitted_from_grouped_breakdown(client, setup, app):
    """Dispatch lines can't be created at zero cartons/packs/pieces through
    the normal API — this proves the service's own defensive filter
    (never trust "no zero-qty rows can exist" as an invariant) by
    exercising stock_service.recipient_totals() directly against a
    deliberately-crafted zero-quantity DispatchLine."""
    compact = _make_product(client, "Notation Zero Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "ZQ-1", cartons=20)

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.dispatch import Dispatch, DispatchLine
        from webapp.services import stock_service

        zero_product = _make_product(client, "Notation Zero Product", {"cartons_to_packs": 10, "packs_to_pieces": 10})
        dispatch = Dispatch.query.filter_by(dispatch_number="ZQ-1").first()
        line = DispatchLine(
            dispatch_id=dispatch.id, product_id=zero_product["id"],
            cartons=0, packs=0, pieces=0, base_unit_qty=0,
            packaging_rule_id=dispatch.lines[0].packaging_rule_id,
        )
        _db.session.add(line)
        _db.session.commit()

        rows = stock_service.recipient_totals("2026-06-29", "2026-07-28", "category")
        row = next(r for r in rows if r["group_name"] == "Notation Category")
        names = {p["product_name"] for p in row["products"]}
        assert "Notation Zero Product" not in names
        assert "Notation Zero Compact" in names


# =====================================================================
# Non-quantity counters unaffected
# =====================================================================

def test_dispatch_counts_remain_unchanged(client, setup):
    compact = _make_product(client, "Notation Count Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "CT-1", cartons=1)
    _finalize(client, compact["id"], setup["customer"]["id"], setup["category"]["id"], "CT-2", cartons=1)

    row = _group(_recipient_totals(client, "category"), "Notation Category")
    assert row["dispatch_count"] == 2


def test_customer_counts_remain_unchanged(client, setup):
    before = client.get("/api/dashboard?date=2026-07-28").get_json()["active_customers"]
    res = client.post("/api/admin/customers", json={
        "name": "Zzyzx Distinctly Unrelated Wholesalers", "sales_category_id": setup["category"]["id"],
        "confirm_not_duplicate": True,
    })
    assert res.status_code == 201, res.get_json()
    after = client.get("/api/dashboard?date=2026-07-28").get_json()["active_customers"]
    assert after == before + 1


def test_draft_counts_remain_unchanged(client, setup):
    compact = _make_product(client, "Notation Draft Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    client.post("/api/dispatches", json={
        "dispatch_number": "DR-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"], "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": compact["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    assert dashboard["draft_dispatches"]["count"] == 1


# =====================================================================
# Low stock — integer base-unit comparisons, unchanged
# =====================================================================

def test_low_stock_comparison_uses_integer_base_units(client, setup):
    compact = _make_product(client, "Notation Low Stock Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    client.patch(f"/api/admin/products/{compact['id']}", json={"low_stock_threshold": 500})
    client.post("/api/daily-figures", json={
        "product_id": compact["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    low = next((r for r in dashboard["low_stock"] if r["product_id"] == compact["id"]), None)
    assert low is not None
    assert isinstance(low["closing_base_qty"], int)


# =====================================================================
# Backend returns per-product integer quantities, no floats
# =====================================================================

def test_recipient_totals_returns_per_product_integer_quantities(client, setup):
    kingmax = _make_product(client, "Notation Integer KingMax", {"carton_to_pieces": 60})
    _finalize(client, kingmax["id"], setup["customer"]["id"], setup["category"]["id"], "INT-1", cartons=5, pieces=3)

    row = _group(_recipient_totals(client, "category"), "Notation Category")
    p = row["products"][0]
    assert isinstance(p["base_qty"], int)
    assert isinstance(p["cartons"], int)
    assert isinstance(p["pieces"], int)
    assert p["product_id"] == kingmax["id"]
    assert p["packaging_rule"] is not None
    assert "e-" not in p["quantity_label"].lower()  # no scientific-notation/float artifact


# =====================================================================
# Performance — no N+1 product/packaging queries
# =====================================================================

def test_recipient_totals_uses_one_batched_product_query_not_one_per_product(client, setup, app):
    products = [
        _make_product(client, f"Notation NPlusOne {i}", {"cartons_to_packs": 10, "packs_to_pieces": 10})
        for i in range(5)
    ]
    for i, p in enumerate(products):
        _finalize(client, p["id"], setup["customer"]["id"], setup["category"]["id"], f"NP-{i}", cartons=i + 1)

    with app.app_context():
        from sqlalchemy import event
        from webapp.extensions import db as _db
        from webapp.services import stock_service

        queries = []
        engine = _db.engine

        def _count(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            stock_service.recipient_totals("2026-06-29", "2026-07-28", "category")
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        product_queries = [q for q in queries if "products" in q.lower() and "select" in q.lower()]
        assert len(product_queries) == 1  # exactly one, regardless of how many distinct products appeared


# =====================================================================
# Mobile-readable markup
# =====================================================================

def test_dashboard_group_breakdown_markup_has_compact_mobile_styling():
    assert ".group-products{" in DASHBOARD_HTML
    assert ".group-product-row{" in DASHBOARD_HTML
    assert 'max-width:640px' in DASHBOARD_HTML  # unchanged mobile-first shell width
