"""
Final review-workflow safety correction, item 1 — detect a concurrent
manual DailyFigure change (Opening Stock, notes, provenance) made after a
product was marked reviewed, not just a Dispatch/Returns/Production book
change (see tests/test_final_correction_review_source_carryforward.py for
that pre-existing check, still passing unmodified).

webapp/services/daily_review_service.py's mark_product_state() snapshots
DailyFigure.updated_at (or None) at review time into the new
DailyReviewProductState.daily_figure_updated_at column; build_summary()
compares that snapshot against the record's CURRENT updated_at on every
call — a mismatch demotes the product back to "not_reviewed" and blocks
submission, exactly like the existing source-book check, without
replacing or duplicating it.
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Concurrency Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


def _save_opening(client, pid, date_str, shift, cartons, notes=None):
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": date_str, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0}, "notes": notes,
    })
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _row_for(summary, pid):
    return next(r for r in summary["products"] if r["product_id"] == pid)


# =====================================================================
# Two Managers reviewing the same product — a concurrent Opening Stock
# correction demotes it and blocks the other reviewer's submission.
# =====================================================================

def test_manager_a_opening_change_after_manager_b_review_blocks_manager_b_submission(client, login_as, setup):
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_b", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 5)
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })
    assert res.status_code == 200

    login_as("mgr_a", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 9)  # a genuine correction, different value

    # Switch back to mgr_b — a plain re-login (login_as would try to
    # re-CREATE mgr_b and hit a UNIQUE constraint on username).
    res = client.post("/api/login", json={"username": "mgr_b", "password": "password123"})
    assert res.status_code == 200, res.get_json()
    stale_summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    row = _row_for(stale_summary, pid)
    assert row["daily_figure_changed_since_review"] is True
    assert row["review_state"] == "not_reviewed"
    assert row["blocks_submission"] is True

    rejected = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert rejected.status_code == 400


def test_reloading_and_re_reviewing_allows_submission(client, login_as, setup):
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_b2", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 5)
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })

    login_as("mgr_a2", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 9)

    res = client.post("/api/login", json={"username": "mgr_b2", "password": "password123"})
    assert res.status_code == 200, res.get_json()
    first_attempt = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert first_attempt.status_code == 400

    # Reload (GET) then re-review, exactly as the frontend's "Reload the
    # current figures before submitting" message instructs.
    reloaded = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    row = _row_for(reloaded, pid)
    assert row["view"]["opening"]["cartons"] == 9  # sees the corrected value, never overwrites it

    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })
    second_attempt = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert second_attempt.status_code == 200


# =====================================================================
# Notes-only changes are detected too — not just Opening Stock quantity.
# =====================================================================

def test_notes_only_change_after_review_is_detected(client, login_as, setup):
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_notes", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 5, notes="original note")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })

    # Same quantity, different notes only.
    _save_opening(client, pid, date_str, shift, 5, notes="updated note")

    summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    row = _row_for(summary, pid)
    assert row["daily_figure_changed_since_review"] is True
    assert row["blocks_submission"] is True

    rejected = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert rejected.status_code == 400


# =====================================================================
# A source-book change is still detected, and never confused with a
# DailyFigure change — the two checks are independent.
# =====================================================================

def test_source_record_change_still_detected_and_not_confused_with_figure_change(client, login_as, setup):
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_src", "password123", "manager")
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()

    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 10, "packs": 0, "pieces": 0}],
    })

    summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    row = _row_for(summary, pid)
    assert row["source_changed_since_review"] is True
    assert row["daily_figure_changed_since_review"] is False  # no DailyFigure row was ever touched
    assert row["blocks_submission"] is True


# =====================================================================
# No false conflicts — nothing changing between review and submit must
# never itself trip either concurrency flag.
# =====================================================================

def test_no_change_between_review_and_submit_does_not_create_a_false_conflict(client, login_as, setup):
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_clean", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 5, notes="steady")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })

    # Reload the review screen a couple of times, exactly like a Manager
    # paging back and forth before submitting — nothing about the record
    # itself changes, so neither flag should ever flip.
    for _ in range(2):
        summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
        row = _row_for(summary, pid)
        assert row["source_changed_since_review"] is False
        assert row["daily_figure_changed_since_review"] is False
        assert row["blocks_submission"] is False

    res = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert res.status_code == 200


def test_live_recalculation_of_derived_fields_is_not_treated_as_a_manual_daily_figure_write(client, login_as, setup):
    """Viewing Daily Figures / the review screen never writes to the
    database (daily_figure_view() is a pure read) — so simply paging
    through a product, or another product's Production/Returns/Dispatch
    activity elsewhere, must never trip this product's own
    daily_figure_changed_since_review flag."""
    pid = setup["product"]["id"]
    date_str, shift = "2026-08-01", "Day"

    login_as("mgr_view", "password123", "manager")
    _save_opening(client, pid, date_str, shift, 5)
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })

    # Repeated reads of the live view and the review summary — never a write.
    for _ in range(3):
        client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}")
        summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
        row = _row_for(summary, pid)
        assert row["daily_figure_changed_since_review"] is False

    res = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert res.status_code == 200
