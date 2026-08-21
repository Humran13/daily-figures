"""
Final pre-deployment correction — Manager/Super Administrator correction
attribution, duplicate-prevention, and correction-reason enforcement
(sections 10-12, 20 of the review-and-submit workflow correction).

Corrections continue to go through the existing, unmodified
upsert_daily_figure() (see webapp/services/stock_service.py) — this file
proves the review workflow's `review_mode` flag doesn't change any of
that write path's identity/uniqueness/attribution guarantees, and that
daily_figure_view()'s new created_by/updated_by attribution fields (added
for this correction) correctly distinguish an Operator's original entry
from a later elevated correction.
"""
import json

import pytest

from webapp.extensions import db as _db


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Attribution Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


def _operator_creates_first_entry(client, login_as, pid, date, shift, cartons):
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("attribution_operator", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200, res.get_json()
    return res.get_json()


# =====================================================================
# Manager/Super Administrator correction updates the SAME record
# =====================================================================

def test_manager_correction_updates_the_existing_daily_figure_record(client, login_as, setup):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    from webapp.models.daily_figure import DailyFigure
    original_id = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first().id

    login_as("attribution_manager", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 15, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    assert res.status_code == 200

    row = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert row.id == original_id
    assert row.opening_cartons == 15
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").count() == 1


def test_super_administrator_correction_updates_the_existing_record(client, login_as, setup):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    from webapp.models.daily_figure import DailyFigure
    original_id = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first().id

    login_as("attribution_admin", "password123", "super_admin")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    row = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert row.id == original_id


# =====================================================================
# Original attribution preserved
# =====================================================================

def test_original_created_by_and_created_at_survive_correction(client, login_as, setup, app):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        from webapp.models.user import User
        original = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
        original_created_by = original.created_by
        original_created_at = original.created_at
        operator_user = User.query.filter_by(username="attribution_operator").first()
        assert original_created_by == operator_user.id

    login_as("attribution_manager2", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 25, "packs": 0, "pieces": 0}, "review_mode": True,
    })

    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        row = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
        assert row.created_by == original_created_by
        assert row.created_at == original_created_at


def test_corrected_by_and_corrected_at_recorded(client, login_as, setup):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    login_as("attribution_manager3", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 30, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["updated_by_username"] == "attribution_manager3"
    assert view["created_by_username"] == "attribution_operator"
    assert view["updated_at"] is not None


def test_history_shows_both_original_operator_and_corrector(client, login_as, setup):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    login_as("attribution_admin2", "password123", "super_admin")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 40, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    history = client.get(f"/api/daily-figures/history?product_id={pid}&date=2026-08-01&shift=Day").get_json()
    assert len(history) == 1  # one official record, not two
    row = history[0]
    assert row["created_by_username"] == "attribution_operator"
    assert row["updated_by_username"] == "attribution_admin2"


# =====================================================================
# No duplicate records of any kind
# =====================================================================

def test_no_second_daily_figure_row_created_by_repeated_corrections(client, login_as, setup):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    login_as("attribution_manager4", "password123", "manager")
    for cartons in (11, 12, 13):
        client.post("/api/daily-figures", json={
            "product_id": pid, "date": "2026-08-01", "shift": "Day",
            "opening": {"cartons": cartons, "packs": 0, "pieces": 0}, "review_mode": True,
        })
    from webapp.models.daily_figure import DailyFigure
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").count() == 1


def test_review_workflow_creates_no_duplicate_production_record(client, login_as, setup):
    pid = setup["product"]["id"]
    p = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    client.post("/api/daily-review/mark-reviewed", json={"date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False})
    from webapp.models.production_record import ProductionRecord
    assert ProductionRecord.query.count() == 1


def test_review_workflow_creates_no_duplicate_returns_record(client, login_as, setup):
    pid = setup["product"]["id"]
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    r = client.post("/api/returns", json={
        "date": "2026-08-01", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    client.post("/api/daily-review/mark-reviewed", json={"date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False})
    from webapp.models.return_record import ReturnRecord
    assert ReturnRecord.query.count() == 1


def test_review_workflow_creates_no_duplicate_dispatch_record(client, login_as, setup):
    pid = setup["product"]["id"]
    category = client.post("/api/admin/sales-categories", json={"name": "Attribution Cat"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Attribution Cust", "sales_category_id": category["id"],
    }).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "ATTR-D1", "date": "2026-08-01", "shift": "Day", "customer_id": customer["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/daily-review/mark-reviewed", json={"date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False})
    from webapp.models.dispatch import Dispatch
    assert Dispatch.query.count() == 1


# =====================================================================
# Correction reason
# =====================================================================

def test_correction_reason_required_before_submission_when_edited(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("attribution_manager5", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": True, "reason": None,
    })
    res = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 400


def test_correction_reason_supplied_allows_submission(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("attribution_manager6", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": True, "reason": "Physical count corrected",
    })
    res = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 200


def test_no_change_review_does_not_require_a_reason(client, login_as, setup):
    pid = setup["product"]["id"]
    login_as("attribution_manager7", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    # Second visit: nothing changed -> reviewed, not edited.
    res = client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    assert res.status_code == 200
    submit_res = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert submit_res.status_code == 200


def test_no_change_submission_does_not_falsely_claim_a_correction(client, login_as, setup, app):
    pid = setup["product"]["id"]
    login_as("attribution_manager8", "password123", "manager")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "edited": False,
    })
    with app.app_context():
        from webapp.models.daily_review_session import DailyReviewProductState
        row = DailyReviewProductState.query.filter_by(product_id=pid).first()
        assert row.state == "reviewed"
        assert row.reason is None


# =====================================================================
# Audit trail: before/after captured, full chain preserved
# =====================================================================

def test_before_and_after_values_are_audited(client, login_as, setup, app):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    login_as("attribution_manager9", "password123", "manager")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="upsert", entity_type="daily_figure").order_by(AuditLog.id.desc()).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        after = json.loads(entry.after_json)
        assert before["opening"]["cartons"] == 10
        assert after["opening"]["cartons"] == 50


def test_multiple_corrections_preserve_the_full_audit_chain(client, login_as, setup, app):
    pid = setup["product"]["id"]
    _operator_creates_first_entry(client, login_as, pid, "2026-08-01", "Day", 10)
    login_as("attribution_manager10", "password123", "manager")
    for cartons in (20, 30, 40):
        client.post("/api/daily-figures", json={
            "product_id": pid, "date": "2026-08-01", "shift": "Day",
            "opening": {"cartons": cartons, "packs": 0, "pieces": 0}, "review_mode": True,
        })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entries = AuditLog.query.filter_by(action="upsert", entity_type="daily_figure").order_by(AuditLog.id.asc()).all()
        assert len(entries) == 4  # 1 original entry + 3 corrections, none overwritten


# =====================================================================
# Permissions
# =====================================================================

def test_operator_cannot_use_review_mode_completion_bypass_meaningfully(client, login_as, setup):
    """review_mode is honored only in the non-Operator branch of the
    upsert route — an Operator sending it anyway still gets the normal
    Operator completion behavior (claims completion), proving Operator
    workflow is genuinely untouched."""
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("attribution_operator2", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0}, "review_mode": True,
    })
    assert res.status_code == 200
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "completed"  # review_mode had no effect for an Operator


def test_viewer_cannot_submit_a_review(client, login_as, setup):
    login_as("attribution_viewer", "password123", "viewer")
    res = client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 403


def test_direct_unauthorized_correction_call_returns_403(client, login_as, setup):
    login_as("attribution_viewer2", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403
