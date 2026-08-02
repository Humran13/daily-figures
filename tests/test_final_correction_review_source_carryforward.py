"""
Final pre-deployment correction — section 21: source-derived values stay
source-derived, and the review workflow's new concurrency check (a
Correct Record edit to a source book after a product was marked reviewed
must demote it back to "needs re-review") is proven directly, alongside
confirmatory checks that universal carry-forward, packaging notation, and
integer-only arithmetic are unaffected by this correction.
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Review Source Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Review Source Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Review Source Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


def _finalize_production(client, pid, date_str, shift, cartons):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


# =====================================================================
# Source books remain the sole source of their figures
# =====================================================================

def test_dispatch_remains_source_of_issued(client, setup):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "SRC-D1", "date": "2026-08-01", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 300
    assert view["issued"]["from_dispatches"] == 300


def test_review_workflow_never_directly_edits_issued_returns_production(client, setup):
    """mark-reviewed only ever touches Opening Stock (via the existing
    upsert route, unchanged) — Returns/Production/Issued stay purely
    derived regardless of any review-workflow call."""
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-08-01", "Day", 4)
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["production"]["base_qty"] == 400  # unaffected by the review call


# =====================================================================
# Concurrency: a source correction after review demotes the product
# =====================================================================

def test_correct_record_source_change_demotes_an_already_reviewed_product(client, setup):
    pid = setup["product"]["id"]
    p = _finalize_production(client, pid, "2026-08-01", "Day", 5)

    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    assert res.status_code == 200
    before = client.get("/api/daily-review?date=2026-08-01&shift=Day").get_json()
    row_before = next(r for r in before["products"] if r["product_id"] == pid)
    assert row_before["review_state"] == "reviewed"
    assert row_before["blocks_submission"] is False

    # Correct Record on the underlying Production entry AFTER the review.
    correct_res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 9, "packs": 0, "pieces": 0}],
    })
    assert correct_res.status_code == 200

    after = client.get("/api/daily-review?date=2026-08-01&shift=Day").get_json()
    row_after = next(r for r in after["products"] if r["product_id"] == pid)
    assert row_after["source_changed_since_review"] is True
    assert row_after["review_state"] == "not_reviewed"
    assert row_after["blocks_submission"] is True


def test_source_change_after_review_blocks_submission_until_re_reviewed(client, setup):
    pid = setup["product"]["id"]
    p = _finalize_production(client, pid, "2026-08-01", "Day", 5)
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 12, "packs": 0, "pieces": 0}],
    })
    res = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 400

    # Re-reviewing clears the flag and unblocks submission.
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    res2 = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert res2.status_code == 200


# =====================================================================
# Historical corrections ripple forward through the review's own live view
# =====================================================================

def test_historical_correction_ripples_into_later_opening_stock(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    p = _finalize_production(client, pid, "2026-07-15", "Day", 5)
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["opening"]["cartons"] == 15

    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "corrected",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 20, "packs": 0, "pieces": 0}],
    })
    later = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert later["opening"]["cartons"] == 30


def test_completed_later_period_shows_recalculated_value_in_review_summary(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    p = _finalize_production(client, pid, "2026-07-15", "Day", 5)
    login_as("src_manager", "password123", "manager")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "corrected",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 25, "packs": 0, "pieces": 0}],
    })
    summary = client.get("/api/daily-review?date=2026-08-01&shift=Day").get_json()
    row = next(r for r in summary["products"] if r["product_id"] == pid)
    assert row["view"]["opening"]["cartons"] == 35


# =====================================================================
# Universal carry-forward / packaging / integer-only regression smoke
# =====================================================================

def test_carry_forward_unaffected_by_review_workflow(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-19", "Day", 20)
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["opening"]["cartons"] == 120


def test_packaging_notation_unaffected_by_review_workflow(client, login_as, setup):
    kingmax = client.post("/api/admin/products", json={"name": "Review Source KingMax"}).get_json()
    client.post(f"/api/admin/products/{kingmax['id']}/packaging-rules", json={"carton_to_pieces": 60})
    pid = kingmax["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 3},
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "5.03 Ctns"


def test_no_floating_point_artifacts_in_review_summary(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 3, "pieces": 7},
    })
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    summary = client.get("/api/daily-review?date=2026-08-01&shift=Day").get_json()
    row = next(r for r in summary["products"] if r["product_id"] == pid)
    assert row["view"]["opening"]["base_qty"] == 137
    assert isinstance(row["view"]["opening"]["base_qty"], int)
