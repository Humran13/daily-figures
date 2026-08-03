"""
Final Dashboard UX correction — regression safety. This round is a
presentation-only change (compact executive layout, three-item previews,
a reusable modal, Attention collapsed by default, and a new
"products worked on today" filter for the Per-Product Daily Figures
preview). None of it may alter any actual calculation — these tests
compare the Dashboard API's numbers directly against the same
stock_service functions it has always called, proving dashboard_service.py
still just shapes/filters the same underlying numbers rather than
recomputing anything.

Universal carry-forward, the Daily Figures review workflow, Reset Daily
Values, and History corrections are exercised end-to-end by their own
dedicated test files (test_stage8_stock_carry_forward.py,
test_final_correction_review_*.py, test_final_correction_reset_modes.py,
test_final_correction_history_records.py, test_final_safety_*.py) — all
still passing unmodified in the full suite, which is the authoritative
proof those areas are untouched. This file adds one direct, Dashboard-
specific confirmation for each, rather than duplicating those suites.
"""
import pytest

from webapp.services import stock_service as svc


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "UX Regression Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


# =====================================================================
# Dashboard figures remain numerically unchanged
# =====================================================================

def test_stock_summary_matches_direct_stock_service_call(client, setup, app):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0},
    })
    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    with app.app_context():
        direct = svc.date_range_summary("2026-07-28", "2026-07-28")
    direct_row = next(r for r in direct if r["product_id"] == pid)
    dash_row = next(r for r in dashboard["stock_summary"] if r["product_id"] == pid)
    assert dash_row["opening_base_qty"] == direct_row["opening_base_qty"]
    assert dash_row["closing_base_qty"] == direct_row["closing_base_qty"]


def test_attention_notices_unchanged_by_this_correction(client, setup, app):
    from webapp.services.dashboard_service import _activity_counts, _attention_notices
    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    with app.app_context():
        activity = _activity_counts("2026-07-28")
        stock_summary = svc.date_range_summary("2026-07-28", "2026-07-28")
        direct_attention = _attention_notices("2026-07-28", activity, stock_summary)
    assert dashboard["attention"] == direct_attention


def test_low_stock_threshold_comparison_unchanged(client, setup):
    pid = setup["product"]["id"]
    client.patch(f"/api/admin/products/{pid}", json={"low_stock_threshold": 500})
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    row = next(r for r in dashboard["low_stock"] if r["product_id"] == pid)
    assert row["closing_base_qty"] <= 500


# =====================================================================
# Universal carry-forward unaffected by the new "worked on today" filter
# =====================================================================

def test_carry_forward_still_ripples_into_stock_summary_even_when_excluded_from_preview(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    prod = client.post("/api/production", json={
        "date": "2026-07-15", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    row = next(r for r in dashboard["stock_summary"] if r["product_id"] == pid)
    assert row["opening_base_qty"] == 1500  # carried forward correctly (10+5 cartons * 100 base units/carton), unchanged calculation

    # Passive on 2026-07-28 specifically (no activity that day) — excluded
    # from the compact preview, but the carry-forward number itself is
    # exactly as it always was.
    assert pid not in {r["product_id"] for r in dashboard["daily_figures_today"]}


# =====================================================================
# Daily Figures review workflow, Reset Daily Values, History corrections
# — untouched by loading the Dashboard
# =====================================================================

def test_loading_dashboard_never_touches_the_review_session(client, setup, app):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-07-28", "shift": "Day", "product_id": pid, "edited": False,
    })
    before = client.get("/api/daily-review?date=2026-07-28&shift=Day").get_json()

    client.get("/api/dashboard?date=2026-07-28")

    after = client.get("/api/daily-review?date=2026-07-28&shift=Day").get_json()
    assert after["session"]["status"] == before["session"]["status"]
    assert after["reviewed_count"] == before["reviewed_count"]


def test_loading_dashboard_never_touches_reset_or_history(client, setup, app):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    before_history = client.get(f"/api/daily-figures/history?product_id={pid}").get_json()

    client.get("/api/dashboard?date=2026-07-28")
    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-28", "shift": "Day", "product_id": pid, "mode": "figures_only",
    }).get_json()

    after_history = client.get(f"/api/daily-figures/history?product_id={pid}").get_json()
    assert after_history == before_history
    assert preview["any_affected"] is True  # reset preview logic itself still functions identically


# =====================================================================
# No raw pieces / raw carton+piece abbreviation appears anywhere in the
# new daily_figures_today preview payload
# =====================================================================

def test_daily_figures_today_never_exposes_raw_carton_piece_abbreviation(client, setup):
    """The dashboard once relied on a stale local formatter that produced
    '5c 3pc' for carton+piece-only products instead of point notation —
    confirm the KingMax-style case renders correctly through this new
    preview's own data too."""
    kingmax = client.post("/api/admin/products", json={"name": "UX Regression KingMax"}).get_json()
    client.post(f"/api/admin/products/{kingmax['id']}/packaging-rules", json={"carton_to_pieces": 60})
    prod = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": kingmax["id"], "cartons": 5, "packs": 0, "pieces": 3}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    dashboard = client.get("/api/dashboard?date=2026-07-28").get_json()
    row = next(r for r in dashboard["daily_figures_today"] if r["product_id"] == kingmax["id"])

    from webapp.services.quantity_format import qty_label
    label = qty_label(row["production"]["cartons"], row["production"]["packs"], row["production"]["pieces"], row["packaging_rule"])
    assert label == "5.03 Ctns"
    assert isinstance(row["production_base_qty"], int)
