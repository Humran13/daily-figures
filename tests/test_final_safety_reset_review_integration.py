"""
Final review-workflow safety correction, item 2 — Reset Daily Values must
also reset the review workflow's own state (DailyReviewSession /
DailyReviewProductState), never leaving the interface claiming a period is
submitted or reviewed after its underlying values were reset out from
under it.

webapp/services/daily_reset_service.py's execute() now calls
webapp/services/daily_review_service.py's clear_product_states_for_reset()
BEFORE its own per-product/source work, in the SAME transaction — a
submitted session is demoted to reopened (never a new session row), and
the targeted products' DailyReviewProductState rows are deleted (so they
read back as "not_reviewed"), with a dedicated audit event. preview() also
now reports the review session's status and each product's review state.
"""
import json

import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product_a = client.post("/api/admin/products", json={"name": "Reset Review Product A"}).get_json()
    client.post(f"/api/admin/products/{product_a['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    product_b = client.post("/api/admin/products", json={"name": "Reset Review Product B"}).get_json()
    client.post(f"/api/admin/products/{product_b['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product_a": product_a, "product_b": product_b}


def _save_opening(client, pid, date_str, shift, cartons):
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": date_str, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200, res.get_json()


def _mark_reviewed(client, pid, date_str, shift):
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid, "edited": False,
    })
    assert res.status_code == 200, res.get_json()


def _row_for(summary, pid):
    return next(r for r in summary["products"] if r["product_id"] == pid)


def _review_row_for(preview, pid):
    return next(r for r in preview["products"] if r["product_id"] == pid)


def _submit_with_both_products_reviewed(client, setup, date_str, shift):
    """Submission requires every ELIGIBLE (active) product to be reviewed
    — both fixture products, not just the one under test — so any test
    that needs a genuinely submitted session must review both first."""
    pid_a, pid_b = setup["product_a"]["id"], setup["product_b"]["id"]
    _save_opening(client, pid_a, date_str, shift, 5)
    _save_opening(client, pid_b, date_str, shift, 5)
    _mark_reviewed(client, pid_a, date_str, shift)
    _mark_reviewed(client, pid_b, date_str, shift)
    res = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert res.status_code == 200, res.get_json()
    return res


# =====================================================================
# Mode A / Mode B both reopen a submitted review
# =====================================================================

def test_mode_a_reopens_a_submitted_review(client, setup):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    submit = _submit_with_both_products_reviewed(client, setup, date_str, shift)
    assert submit.get_json()["status"] == "submitted"

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart", "mode": "figures_only",
    })
    assert res.status_code == 200

    after = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert after["session"]["status"] == "reopened"
    assert "Reset Daily Values" in after["session"]["reopen_reason"]


def test_mode_b_reopens_a_submitted_review(client, setup):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _submit_with_both_products_reviewed(client, setup, date_str, shift)

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": f"FULL RESET {date_str} DAY",
    })
    assert res.status_code == 200

    after = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert after["session"]["status"] == "reopened"


# =====================================================================
# One-product vs all-products scope
# =====================================================================

def test_one_product_reset_clears_only_that_products_review_state(client, setup):
    pid_a, pid_b = setup["product_a"]["id"], setup["product_b"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _save_opening(client, pid_a, date_str, shift, 5)
    _save_opening(client, pid_b, date_str, shift, 5)
    _mark_reviewed(client, pid_a, date_str, shift)
    _mark_reviewed(client, pid_b, date_str, shift)

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid_a, "reason": "restart A", "mode": "figures_only",
    })
    assert res.status_code == 200

    summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert _row_for(summary, pid_a)["review_state"] == "not_reviewed"
    assert _row_for(summary, pid_b)["review_state"] == "reviewed"  # preserved
    assert summary["session"]["status"] == "in_progress"  # was never submitted, so nothing to reopen


def test_all_products_reset_clears_every_product_state(client, setup):
    pid_a, pid_b = setup["product_a"]["id"], setup["product_b"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _save_opening(client, pid_a, date_str, shift, 5)
    _save_opening(client, pid_b, date_str, shift, 5)
    _mark_reviewed(client, pid_a, date_str, shift)
    _mark_reviewed(client, pid_b, date_str, shift)

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "reason": "restart all", "mode": "figures_only",
    })
    assert res.status_code == 200

    summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert _row_for(summary, pid_a)["review_state"] == "not_reviewed"
    assert _row_for(summary, pid_b)["review_state"] == "not_reviewed"


# =====================================================================
# Isolation: other dates/shifts untouched; no duplicate session
# =====================================================================

def test_other_dates_and_shifts_remain_unchanged(client, setup):
    pid = setup["product_a"]["id"]
    shift = "Day"
    _save_opening(client, pid, "2026-08-01", shift, 5)
    _mark_reviewed(client, pid, "2026-08-01", shift)
    _save_opening(client, pid, "2026-08-02", shift, 5)
    _mark_reviewed(client, pid, "2026-08-02", shift)

    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": shift, "product_id": pid, "reason": "restart", "mode": "figures_only",
    })
    assert res.status_code == 200

    untouched = client.get(f"/api/daily-review?date=2026-08-02&shift={shift}").get_json()
    assert _row_for(untouched, pid)["review_state"] == "reviewed"


def test_existing_review_session_id_preserved_no_duplicate_created(client, setup, app):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    submit = _submit_with_both_products_reviewed(client, setup, date_str, shift)
    session_id_before = submit.get_json()["id"]

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart", "mode": "figures_only",
    })
    assert res.status_code == 200

    after = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert after["session"]["id"] == session_id_before

    with app.app_context():
        from webapp.models.daily_review_session import DailyReviewSession
        assert DailyReviewSession.query.filter_by(date=date_str, shift=shift).count() == 1


# =====================================================================
# Audit trail
# =====================================================================

def test_previous_submission_history_remains_in_audit_log(client, setup, app):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _submit_with_both_products_reviewed(client, setup, date_str, shift)

    client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart", "mode": "figures_only",
    })

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        submit_entries = AuditLog.query.filter_by(action="submit_review").all()
        assert len(submit_entries) == 1  # the original submission is still there, never deleted

        reset_review_entries = AuditLog.query.filter_by(action="reset_review_state").all()
        assert len(reset_review_entries) == 1
        after = json.loads(reset_review_entries[0].after_json)
        assert after["previous_review_status"] == "submitted"
        assert after["new_review_status"] == "reopened"
        assert after["reset_mode"] == "figures_only"
        assert after["reason"] == "restart"
        assert any(c["product_id"] == pid for c in after["cleared_product_states"])
        assert reset_review_entries[0].username == "root"


# =====================================================================
# Reset preview reports affected review state
# =====================================================================

def test_reset_preview_reports_affected_review_state(client, setup):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _submit_with_both_products_reviewed(client, setup, date_str, shift)

    preview = client.post("/api/daily-reset/preview", json={
        "date": date_str, "shift": shift, "product_id": pid, "mode": "figures_only",
    }).get_json()
    assert preview["review_session_status"] == "submitted"
    assert preview["affects_submitted_review"] is True
    assert preview["affects_in_progress_review"] is False
    assert _review_row_for(preview, pid)["review_state"] == "reviewed"
    assert preview["any_affected"] is True


def test_reset_preview_shows_edited_and_skipped_states(client, setup):
    pid_a, pid_b = setup["product_a"]["id"], setup["product_b"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _save_opening(client, pid_a, date_str, shift, 5)
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": date_str, "shift": shift, "product_id": pid_a, "edited": True, "reason": "correcting typo",
    })
    assert res.status_code == 200
    res = client.post("/api/daily-review/mark-skipped", json={
        "date": date_str, "shift": shift, "product_id": pid_b,
    })
    assert res.status_code == 200

    preview = client.post("/api/daily-reset/preview", json={
        "date": date_str, "shift": shift, "mode": "figures_only",
    }).get_json()
    assert _review_row_for(preview, pid_a)["review_state"] == "edited"
    assert _review_row_for(preview, pid_b)["review_state"] == "skipped"
    assert preview["affects_in_progress_review"] is True


# =====================================================================
# Transaction safety — a later failure rolls back review-state changes too
# =====================================================================

def test_failed_reset_rolls_back_review_session_changes(client, setup, app, monkeypatch):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _submit_with_both_products_reviewed(client, setup, date_str, shift)

    from webapp.services import daily_reset_service

    def _boom(*a, **kw):
        raise daily_reset_service.DailyResetError("simulated source-book failure")

    # clear_product_states_for_reset() runs BEFORE this per-product source
    # step, in the same uncommitted transaction — forcing a failure here
    # proves the earlier (already in-memory) review-session demotion and
    # product-state deletion get rolled back too, not just "never attempted".
    monkeypatch.setattr(daily_reset_service, "_neutralize_source_for_product", _boom)

    res = client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": f"FULL RESET {date_str} DAY",
    })
    assert res.status_code == 400

    after = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert after["session"]["status"] == "submitted"  # never demoted
    row = _row_for(after, pid)
    assert row["review_state"] == "reviewed"  # never cleared

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        assert AuditLog.query.filter_by(action="reset_review_state").count() == 0


# =====================================================================
# Post-reset workflow: replacement entries require fresh review, and
# final submission is blocked until reset products are reviewed again.
# =====================================================================

def test_replacement_entries_require_fresh_review(client, setup):
    pid = setup["product_a"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _save_opening(client, pid, date_str, shift, 5)
    _mark_reviewed(client, pid, date_str, shift)

    client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid, "reason": "restart", "mode": "figures_only",
    })

    summary = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert _row_for(summary, pid)["review_state"] == "not_reviewed"

    # A replacement entry is accepted normally...
    _save_opening(client, pid, date_str, shift, 8)
    # ...but still requires a fresh review before it counts.
    still_pending = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert _row_for(still_pending, pid)["review_state"] == "not_reviewed"

    _mark_reviewed(client, pid, date_str, shift)
    reviewed = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert _row_for(reviewed, pid)["review_state"] == "reviewed"


def test_final_submission_blocked_until_reset_products_reviewed_again(client, setup):
    pid_a, pid_b = setup["product_a"]["id"], setup["product_b"]["id"]
    date_str, shift = "2026-08-01", "Day"
    _save_opening(client, pid_a, date_str, shift, 5)
    _save_opening(client, pid_b, date_str, shift, 5)
    _mark_reviewed(client, pid_a, date_str, shift)
    _mark_reviewed(client, pid_b, date_str, shift)

    client.post("/api/daily-reset", json={
        "date": date_str, "shift": shift, "product_id": pid_a, "reason": "restart A", "mode": "figures_only",
    })

    # Reset wipes product A's review row entirely (review_state ->
    # "not_reviewed" via absence, not staleness) — a genuinely unreviewed
    # product, not a hard blocker, so this is now the "confirm to submit
    # anyway" 409 rather than an unconditional 400 (see the final UX/
    # reporting package's Manager/Super Admin unreviewed-submit
    # confirmation). It still isn't submitted without that confirmation.
    blocked = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert blocked.status_code == 409
    assert blocked.get_json()["requires_confirmation"] is True
    still_unsubmitted = client.get(f"/api/daily-review?date={date_str}&shift={shift}").get_json()
    assert still_unsubmitted["session"]["status"] == "in_progress"

    _save_opening(client, pid_a, date_str, shift, 6)
    _mark_reviewed(client, pid_a, date_str, shift)
    allowed = client.post("/api/daily-review/submit", json={"date": date_str, "shift": shift})
    assert allowed.status_code == 200
