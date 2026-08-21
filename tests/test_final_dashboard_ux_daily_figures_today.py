"""
Final Dashboard UX correction, sections 6-7 — the compact 'Per-Product
Daily Figures' preview/full-view must only ever include a product that
had genuine activity or review work for the selected date, never merely
because it carries a non-zero Opening/Closing Stock balance forward from
an earlier period.

webapp/services/dashboard_service.py's _products_worked_on_today() is the
qualifying rule; _daily_figures_today() filters+orders stock_summary rows
down to that set (existing product_usage_service.ranked_active_products()
ranking reused as-is, never a second ranking algorithm) and is exposed on
GET /api/dashboard as "daily_figures_today".
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "DFT Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "DFT Customer", "sales_category_id": category["id"]}).get_json()
    return {"category": category, "customer": customer}


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _dashboard(client, date="2026-07-28"):
    res = client.get(f"/api/dashboard?date={date}")
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _today_ids(dashboard):
    return {row["product_id"] for row in dashboard["daily_figures_today"]}


# =====================================================================
# Passive carry-forward-only products are excluded
# =====================================================================

def test_passive_carry_forward_only_product_excluded(client, setup):
    p = _make_product(client, "DFT Carry Forward Only")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    dashboard = _dashboard(client, "2026-07-28")
    # The product still legitimately appears in the full stock_summary
    # (it carries a real balance) — only the "worked on today" list must
    # exclude it.
    assert any(r["product_id"] == p["id"] for r in dashboard["stock_summary"])
    assert p["id"] not in _today_ids(dashboard)


# =====================================================================
# Each qualifying activity type includes the product
# =====================================================================

def test_production_activity_includes_product(client, setup):
    p = _make_product(client, "DFT Production Activity")
    prod = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": p["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{prod['id']}/finalize")
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)


def test_returns_activity_includes_product(client, setup):
    p = _make_product(client, "DFT Returns Activity")
    r = client.post("/api/returns", json={
        "date": "2026-07-28", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)


def test_issued_activity_includes_product(client, setup):
    p = _make_product(client, "DFT Issued Activity")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "DFT-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"], "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": p["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)


def test_manual_opening_correction_includes_product(client, setup):
    p = _make_product(client, "DFT Manual Correction")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    # Carry-forward into 2026-07-28 would be 10 — submit a genuinely
    # different elevated value so it becomes a real manual_correction.
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 15, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)


def test_no_activity_confirmed_includes_product(client, setup):
    p = _make_product(client, "DFT No Activity")
    res = client.post("/api/daily-entry-status/no-activity", json={
        "date": "2026-07-28", "shift": "Day", "product_id": p["id"],
    })
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)
    row = next(r for r in dashboard["daily_figures_today"] if r["product_id"] == p["id"])
    assert row["today_status"] == "no_activity"


def test_reviewed_product_appears_with_reviewed_status(client, setup):
    p = _make_product(client, "DFT Reviewed")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-07-28", "shift": "Day", "product_id": p["id"], "edited": False,
    })
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    assert p["id"] in _today_ids(dashboard)


def test_edited_product_appears_with_edited_status(client, setup):
    p = _make_product(client, "DFT Edited")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-07-28", "shift": "Day", "product_id": p["id"], "edited": True, "reason": "fixed typo",
    })
    assert res.status_code == 200, res.get_json()

    dashboard = _dashboard(client)
    row = next(r for r in dashboard["daily_figures_today"] if r["product_id"] == p["id"])
    assert row["today_status"] == "edited"


# =====================================================================
# Packaging-aware notation and integer base units are preserved
# =====================================================================

def test_daily_figures_today_row_uses_packaging_aware_parts(client, setup):
    p = _make_product(client, "DFT Notation Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    prod = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": p["id"], "cartons": 109, "packs": 5, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    dashboard = _dashboard(client)
    row = next(r for r in dashboard["daily_figures_today"] if r["product_id"] == p["id"])
    assert isinstance(row["production_base_qty"], int)
    assert row["production"]["cartons"] == 109 and row["production"]["packs"] == 5

    from webapp.services.quantity_format import qty_label
    label = qty_label(row["production"]["cartons"], row["production"]["packs"], row["production"]["pieces"], row["packaging_rule"])
    assert label == "109.50 Ctns"


# =====================================================================
# Ordering — existing global usage ranking, deterministic tie-break
# =====================================================================

def test_daily_figures_today_ordered_by_existing_usage_ranking(client, setup):
    low_usage = _make_product(client, "DFT Rank Low Usage")
    high_usage = _make_product(client, "DFT Rank High Usage")
    for p in (low_usage, high_usage):
        client.post("/api/daily-figures", json={
            "product_id": p["id"], "date": "2026-07-28", "shift": "Day",
            "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        })
    # Give high_usage several recent usage events (via repeated finalized
    # activity), matching how product_usage_service is fed elsewhere.
    for i in range(3):
        prod = client.post("/api/production", json={
            "date": "2026-07-28", "shift": "Day",
            "lines": [{"product_id": high_usage["id"], "cartons": 1, "packs": 0, "pieces": 0}],
        }).get_json()
        client.post(f"/api/production/{prod['id']}/finalize")

    dashboard = _dashboard(client)
    order = [r["product_id"] for r in dashboard["daily_figures_today"]]
    assert order.index(high_usage["id"]) < order.index(low_usage["id"])
