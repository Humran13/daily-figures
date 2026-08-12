"""
Targeted dashboard enhancement — "Top Issued Products — Last 30 Days".

Added immediately after the existing "Top Issued Products — Last 7 Days"
card, reusing THE SAME dashboard_service._top_products() query (now
parameterized by window_days — see its own docstring) for both windows:
same authoritative Dispatch/DispatchLine data source, same
STATUS_FINALIZED filter, same product aggregation, same packaging-aware
formatting (svc.from_base_units() + Product.current_packaging_rule()),
same SQL-level "sort before limit" behavior, same frontend
renderPreviewSection()/topProductRow() renderer, same role visibility
(GET /api/dashboard is unchanged — still ROLE_SUPER_ADMIN/ROLE_MANAGER/
ROLE_VIEWER). Business dates (Dispatch.date) throughout, never
created_at.
"""
import pathlib

import pytest

from webapp.services.business_calendar import business_today

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("t30_root", "password123", "super_admin")


def _make_product(client, name="T30 Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "T30 Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "T30 Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _dispatch(client, setup, product_id, date, cartons, number, finalize=True):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    if finalize:
        client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


def _days_ago(n):
    import datetime
    return (datetime.date.fromisoformat(business_today()) - datetime.timedelta(days=n)).isoformat()


# =====================================================================
# 1-2. Markup presence + immediate position after the 7-day card
# =====================================================================

def test_dashboard_contains_both_top_issued_cards():
    assert "<h2>Top issued products (last 7 days)</h2>" in DASHBOARD_HTML
    assert "<h2>Top issued products (last 30 days)</h2>" in DASHBOARD_HTML


def test_30_day_card_appears_immediately_after_7_day_card():
    idx_7d = DASHBOARD_HTML.index('id="topProducts"')
    idx_30d = DASHBOARD_HTML.index('id="topProducts30d"')
    idx_category = DASHBOARD_HTML.index('id="byCategory"')
    assert idx_7d < idx_30d < idx_category
    # "Immediately after" — no other <div class="card"> boundary sits
    # between the two Top Issued cards.
    between = DASHBOARD_HTML[idx_7d:idx_30d]
    assert between.count('<div class="card">') == 1  # only the 30-day card's own opening tag


def test_existing_cards_not_removed_or_redesigned():
    assert '<h2>Issued by sales category (last 30 days)</h2>' in DASHBOARD_HTML
    assert '<h2>Issued by recipient (last 30 days)</h2>' in DASHBOARD_HTML
    assert 'id="byCategory"' in DASHBOARD_HTML
    assert 'id="byRecipient"' in DASHBOARD_HTML


# =====================================================================
# 3-4. Date range: outside/inside the 30-day window
# =====================================================================

def test_dispatch_outside_30_days_excluded(client, setup):
    today = business_today()
    old_date = _days_ago(31)
    _dispatch(client, setup, setup["product"]["id"], old_date, 5, "T30-OLD-1")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert dash["top_products_30d"] == []


def test_dispatch_inside_30_days_included(client, setup):
    today = business_today()
    recent_date = _days_ago(29)
    _dispatch(client, setup, setup["product"]["id"], recent_date, 5, "T30-RECENT-1")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert any(p["product_id"] == setup["product"]["id"] for p in dash["top_products_30d"])
    row = next(p for p in dash["top_products_30d"] if p["product_id"] == setup["product"]["id"])
    assert row["base_qty"] == 500


def test_30_day_window_boundary_is_inclusive_of_day_29_exclusive_of_day_31(client, setup):
    today = business_today()
    boundary_date = _days_ago(29)  # window_start = today - 29 days, inclusive
    just_outside = _days_ago(30)
    p_in = _make_product(client, "T30 Boundary In")
    p_out = _make_product(client, "T30 Boundary Out")
    _dispatch(client, setup, p_in["id"], boundary_date, 2, "T30-BOUND-IN")
    _dispatch(client, setup, p_out["id"], just_outside, 2, "T30-BOUND-OUT")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    ids = {p["product_id"] for p in dash["top_products_30d"]}
    assert p_in["id"] in ids
    assert p_out["id"] not in ids


# =====================================================================
# 5-7. Status filtering (draft / void / finalized)
# =====================================================================

def test_draft_dispatch_excluded_from_30_day_card(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-DRAFT-1", finalize=False)
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert dash["top_products_30d"] == []


def test_void_dispatch_excluded_from_30_day_card(client, setup):
    today = business_today()
    d = _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-VOID-1")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "entered in error"})
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert dash["top_products_30d"] == []


def test_finalized_dispatch_included_in_30_day_card(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-FIN-1")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert any(p["product_id"] == setup["product"]["id"] and p["base_qty"] == 500 for p in dash["top_products_30d"])


# =====================================================================
# 8. Aggregation per product
# =====================================================================

def test_totals_aggregated_correctly_per_product_across_multiple_dispatches(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 3, "T30-AGG-1")
    _dispatch(client, setup, setup["product"]["id"], _days_ago(10), 4, "T30-AGG-2")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    row = next(p for p in dash["top_products_30d"] if p["product_id"] == setup["product"]["id"])
    assert row["base_qty"] == 700  # (3 + 4) cartons * 100 base units/carton


# =====================================================================
# 9-11. Sorting — numerically descending, sorted BEFORE limiting, View
# All preserves the same order
# =====================================================================

def test_30_day_results_sorted_numerically_descending(client, setup):
    today = business_today()
    qtys = [("A", 3000), ("B", 77), ("C", 1028), ("D", 200), ("E", 8)]
    for name, qty in qtys:
        p = _make_product(client, f"T30-Sort-{name}")
        _dispatch(client, setup, p["id"], today, qty, f"T30-SORT-{name}")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    names = [p["product_name"] for p in dash["top_products_30d"]]
    assert names == ["T30-Sort-A", "T30-Sort-C", "T30-Sort-D", "T30-Sort-B", "T30-Sort-E"]
    quantities = [p["base_qty"] for p in dash["top_products_30d"]]
    assert quantities == sorted(quantities, reverse=True)


def test_preview_rows_selected_after_sorting_the_complete_result(client, setup):
    today = business_today()
    qtys = [10, 500, 5, 3000, 1, 200, 8, 1028]
    for i, qty in enumerate(qtys):
        p = _make_product(client, f"T30-Limit-{i}")
        _dispatch(client, setup, p["id"], today, qty, f"T30-LIMIT-{i}")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    top5 = [p["base_qty"] for p in dash["top_products_30d"]]
    assert len(top5) == 5
    assert top5 == sorted([q * 100 for q in qtys], reverse=True)[:5]


def test_view_all_preserves_same_descending_order_no_second_fetch():
    # "View all" for the 30-day card reuses the exact same already-fetched
    # `top30d` array via the shared renderPreviewSection() helper — never
    # a second request, never a client-side re-sort.
    idx = DASHBOARD_HTML.index("// ---- 4b. Top issued products (30 days)")
    end = DASHBOARD_HTML.index("});", idx) + 3
    block = DASHBOARD_HTML[idx:end]
    assert "const top30d = data.top_products_30d || [];" in block
    assert "renderPreviewSection(document.getElementById('topProducts30d'), top30d, topProductRow" in block
    assert ".sort(" not in block


# =====================================================================
# 12. Packaging formatting matches the 7-day card (same product, both
# windows overlapping — identical formatter, identical output shape)
# =====================================================================

def test_packaging_formatting_matches_7_day_card_exactly(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 6, "T30-PKG-1")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    row_7d = next(p for p in dash["top_products"] if p["product_id"] == setup["product"]["id"])
    row_30d = next(p for p in dash["top_products_30d"] if p["product_id"] == setup["product"]["id"])
    assert row_7d["cartons"] == row_30d["cartons"] == 6
    assert row_7d["packs"] == row_30d["packs"] == 0
    assert row_7d["pieces"] == row_30d["pieces"] == 0
    assert row_7d["packaging_rule"] == row_30d["packaging_rule"]


def test_napkins_standard_packaging_rule_correct_in_30_day_card(client, setup):
    napkins = client.post("/api/admin/products", json={"name": "Napkins Standard T30"}).get_json()
    client.post(f"/api/admin/products/{napkins['id']}/packaging-rules", json={
        "cartons_to_packs": 6, "packs_to_pieces": 10,
    })
    today = business_today()
    _dispatch(client, setup, napkins["id"], today, 2, "T30-NAPKIN-1")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    row = next(p for p in dash["top_products_30d"] if p["product_id"] == napkins["id"])
    assert row["base_qty"] == 2 * 6 * 10  # 2 cartons * 6 packs/carton * 10 pieces/pack
    assert row["cartons"] == 2 and row["packs"] == 0 and row["pieces"] == 0


# =====================================================================
# 13-15. Role visibility
# =====================================================================

def test_manager_sees_the_30_day_card(client, setup, login_as):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-MGR-1")
    client.post("/api/logout")
    login_as("t30_mgr", "password123", "manager")
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    assert dash["top_products_30d"][0]["product_id"] == setup["product"]["id"]


def test_viewer_sees_the_same_read_only_30_day_card_data(client, setup, login_as):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-VIEW-1")

    login_as("t30_mgr2", "password123", "manager")
    manager_dash = client.get(f"/api/dashboard?date={today}").get_json()
    client.post("/api/logout")

    login_as("t30_viewer", "password123", "viewer")
    viewer_dash = client.get(f"/api/dashboard?date={today}").get_json()

    manager_dash.pop("generated_at", None)
    viewer_dash.pop("generated_at", None)
    assert manager_dash["top_products_30d"] == viewer_dash["top_products_30d"]


def test_viewer_permissions_unchanged_no_quick_actions_no_write(client, setup, login_as):
    today = business_today()
    login_as("t30_viewer2", "password123", "viewer")
    assert client.get(f"/api/dashboard?date={today}").status_code == 200
    # Still no write access anywhere — this is reporting only.
    d = client.post("/api/dispatches", json={
        "dispatch_number": "T30-VIEWER-FORBIDDEN", "date": today, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert d.status_code == 403
    body = re_search_quick_actions()
    assert "role === 'viewer'" in body


def re_search_quick_actions():
    idx = DASHBOARD_HTML.index("function renderQuickActions(role){")
    end = DASHBOARD_HTML.index("\n}\n", idx)
    return DASHBOARD_HTML[idx:end]


# =====================================================================
# 16-17. No regression to the 7-day card or the Category/Recipient cards
# =====================================================================

def test_7_day_card_not_regressed(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 5, "T30-REG7-1")
    _dispatch(client, setup, setup["product"]["id"], _days_ago(10), 5, "T30-REG7-2")  # outside 7d, inside 30d
    dash = client.get(f"/api/dashboard?date={today}").get_json()
    row_7d = next(p for p in dash["top_products"] if p["product_id"] == setup["product"]["id"])
    row_30d = next(p for p in dash["top_products_30d"] if p["product_id"] == setup["product"]["id"])
    assert row_7d["base_qty"] == 500    # only the in-window dispatch
    assert row_30d["base_qty"] == 1000  # both dispatches
    assert dash["top_products_window_days"] == 7
    assert dash["top_products_30d_window_days"] == 30


def test_sales_category_and_recipient_cards_not_regressed(client, setup):
    today = business_today()
    _dispatch(client, setup, setup["product"]["id"], today, 4, "T30-REGCR-1")
    cat_totals = client.get(f"/api/reports/recipient-totals?date_from={today}&date_to={today}&group_by=category").get_json()
    rec_totals = client.get(f"/api/reports/recipient-totals?date_from={today}&date_to={today}&group_by=recipient").get_json()
    assert any(r["total_issued_base_qty"] == 400 for r in cat_totals)
    assert any(r["total_issued_base_qty"] == 400 for r in rec_totals)
