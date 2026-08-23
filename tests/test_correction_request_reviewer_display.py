"""
Correction Requests card display: an APPROVED (active-grant) card must show
WHO granted the edit permission, not just when it expires. Backend already
serializes the granting/reviewing user via CorrectionRequest.reviewed_by /
.reviewer (webapp/models/correction_request.py's to_dict() -> "reviewed_by_
username") for every status, including approved and expired — this was
never returned to the frontend before this round, only rendered for
"rejected". This module covers the resulting static/requests.html card
markup for every status, and confirms no permission/duration/timezone
behavior changed.
"""
import datetime

import pytest

from webapp.services.business_calendar import utcnow

import pathlib

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
REQUESTS_HTML = (STATIC / "requests.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("rd_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "RD Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "RD Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "RD Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _relogin(client, username, password="password123"):
    client.post("/api/logout")
    res = client.post("/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.get_json()


def _historical_dispatch(client, app, setup, number, cartons=5):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        row = db.session.get(Dispatch, d["id"])
        row.created_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    return d


def _request_and_review(client, app, setup, login_as, operator, reviewer, reviewer_role,
                         dispatch_number, decision="approve", review_note=None):
    login_as(operator, "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Wrong quantity, needs a correction here",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"],
                                "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    login_as(reviewer, "password123", reviewer_role)
    body = {"review_note": review_note} if review_note else {}
    res = client.post(f"/api/correction-requests/{req['id']}/{decision}", json=body)
    assert res.status_code == 200, res.get_json()
    reviewed = res.get_json()
    client.post("/api/logout")
    return d, reviewed


# =====================================================================
# CARD MARKUP: the renderRequestRow() logic in requests.html
# =====================================================================

def test_requests_html_renders_edit_granted_by_for_an_approved_card():
    assert "Edit granted by" in REQUESTS_HTML
    assert "${escapeHtml(r.reviewed_by_username || '—')}" in REQUESTS_HTML


def test_requests_html_still_renders_active_until_for_an_approved_card():
    assert "Active until" in REQUESTS_HTML
    assert "r.grant_expires_at_label" in REQUESTS_HTML


def test_requests_html_expired_card_shows_original_granter():
    idx = REQUESTS_HTML.index("r.status === 'expired' ?")
    block = REQUESTS_HTML[idx:idx + 200]
    assert "Edit granted by" in block
    assert "reviewed_by_username" in block


def test_requests_html_completed_card_shows_reviewer():
    idx = REQUESTS_HTML.index("r.status === 'completed' ?")
    block = REQUESTS_HTML[idx:idx + 300]
    assert "Reviewed by" in block
    assert "reviewed_by_username" in block
    assert "Correction submitted" in block  # existing wording preserved


def test_requests_html_rejected_card_still_shows_reviewer_and_note():
    idx = REQUESTS_HTML.index("r.status === 'rejected' ?")
    block = REQUESTS_HTML[idx:idx + 300]
    assert "Reviewed by" in block
    assert "reviewed_by_username" in block
    assert "review_note" in block


def test_requester_display_line_unchanged():
    assert "Operator: ${escapeHtml(r.requested_by_username || '—')}" in REQUESTS_HTML


# =====================================================================
# API: the granting reviewer is actually returned for the approved card
# =====================================================================

def test_approved_request_includes_the_actual_approving_reviewer_username(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_mgr", reviewer="rd_mgr", reviewer_role="manager",
        dispatch_number="RD-MGR-1",
    )
    assert approved["status"] == "approved"
    assert approved["reviewed_by_username"] == "rd_mgr"
    assert approved["grant_expires_at_label"] is not None


def test_manager_approval_shows_managers_actual_username(client, app, setup, login_as):
    _, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_m2", reviewer="rd_manager_x", reviewer_role="manager",
        dispatch_number="RD-MGR-2",
    )
    assert approved["reviewed_by_username"] == "rd_manager_x"
    assert approved["reviewed_by_username"] != "manager"


def test_accountant_approval_shows_accountants_actual_username(client, app, setup, login_as):
    _, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_a2", reviewer="rd_accountant_x", reviewer_role="accountant",
        dispatch_number="RD-ACC-1",
    )
    assert approved["reviewed_by_username"] == "rd_accountant_x"
    assert approved["reviewed_by_username"] != "accountant"


def test_super_admin_approval_shows_super_admins_actual_username(client, app, setup, login_as):
    _, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_s2", reviewer="rd_super_x", reviewer_role="super_admin",
        dispatch_number="RD-SA-1",
    )
    assert approved["reviewed_by_username"] == "rd_super_x"
    assert approved["reviewed_by_username"] != "super_admin"


def test_rejected_request_still_shows_its_reviewer(client, app, setup, login_as):
    _, rejected = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_rej", reviewer="rd_rej_mgr", reviewer_role="manager",
        dispatch_number="RD-REJ-1", decision="reject", review_note="Not enough detail",
    )
    assert rejected["status"] == "rejected"
    assert rejected["reviewed_by_username"] == "rd_rej_mgr"


def test_completed_request_still_shows_its_reviewer(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_comp", reviewer="rd_comp_mgr", reviewer_role="manager",
        dispatch_number="RD-COMP-1",
    )
    _relogin(client, "rd_op_comp")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using my grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"],
                                                 "cartons": 3, "packs": 0, "pieces": 0}],
    })
    _relogin(client, "rd_comp_mgr")
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}").get_json()
    match = next(r for r in rows if r["record_id"] == d["id"])
    assert match["status"] == "completed"
    assert match["reviewed_by_username"] == "rd_comp_mgr"


def test_expired_approved_grant_retains_original_approver_accountability(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_exp", reviewer="rd_exp_mgr", reviewer_role="manager",
        dispatch_number="RD-EXP-1",
    )
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.correction_request import CorrectionRequest
        row = db.session.get(CorrectionRequest, approved["id"])
        row.reviewed_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    _relogin(client, "rd_exp_mgr")
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}").get_json()
    match = next(r for r in rows if r["record_id"] == d["id"])
    assert match["status"] == "expired"
    # The original approving reviewer is preserved — no second reviewer invented.
    assert match["reviewed_by_username"] == "rd_exp_mgr"


def test_kampala_timestamp_formatting_unchanged_for_approved_grant(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_tz", reviewer="rd_tz_mgr", reviewer_role="manager",
        dispatch_number="RD-TZ-1",
    )
    from webapp.services.business_calendar import format_kampala_datetime
    # Directly recompute the expected label from the stored grant expiry
    # and confirm the API returned exactly that Kampala-formatted string —
    # proves this change didn't touch grant-duration/timezone logic at all.
    assert approved["grant_expires_at_label"] == format_kampala_datetime(
        __import__("datetime").datetime.fromisoformat(approved["grant_expires_at"])
    )


def test_grant_duration_still_exactly_24_hours(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_dur", reviewer="rd_dur_mgr", reviewer_role="manager",
        dispatch_number="RD-DUR-1",
    )
    reviewed_at = __import__("datetime").datetime.fromisoformat(approved["reviewed_at"])
    expires_at = __import__("datetime").datetime.fromisoformat(approved["grant_expires_at"])
    assert expires_at - reviewed_at == datetime.timedelta(hours=24)


def test_grant_still_permits_exactly_one_correction_after_display_change(client, app, setup, login_as):
    d, approved = _request_and_review(
        client, app, setup, login_as,
        operator="rd_op_one", reviewer="rd_one_mgr", reviewer_role="manager",
        dispatch_number="RD-ONE-1",
    )
    _relogin(client, "rd_op_one")
    first = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "first use", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"],
                                            "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert first.status_code == 200
    second = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "second attempt", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"],
                                                 "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert second.status_code == 403


def test_operator_still_cannot_approve_or_reject(client, app, setup, login_as):
    login_as("rd_op_perm", "password123", "operator")
    d = _historical_dispatch(client, app, setup, "RD-PERM-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a correction here",
        "payload": {"lines": []},
    }).get_json()
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 403
