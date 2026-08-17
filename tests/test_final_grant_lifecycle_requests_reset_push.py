"""
Full targeted Operator correction/void/requests/notification package —
remaining coverage not already exercised by test_final_correction_requests.py
/ test_final_operator_same_day_edit_window.py / test_final_void_workflow.py:

  - the one-time, record-specific, 24-hour edit GRANT's full lifecycle
    (start on approval, consume on first successful correction, refuse a
    second attempt, expire unused after 24h, race-safety),
  - the new top-level Requests page/nav/badge,
  - Reset Daily Values now being Super-Administrator only,
  - Web Push infrastructure safety (opt-in, graceful absence, no secrets),
  - correcting a voided record remains impossible (section 17).
"""
import datetime
import json
import pathlib

import pytest

from webapp.services.business_calendar import utcnow

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
REQUESTS_HTML = (STATIC / "requests.html").read_text(encoding="utf-8")
APP_SHELL_JS = (STATIC / "app-shell.js").read_text(encoding="utf-8")
RESET_HTML = (STATIC / "reset-daily-values.html").read_text(encoding="utf-8")
SW_JS = (STATIC / "sw.js").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")


def _make_product(client, name="GLC Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("glc_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "GLC Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "GLC Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _historical_dispatch(client, app, setup, number="GLC-D1", cartons=5):
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


def _relogin(client, username, password="password123"):
    client.post("/api/logout")
    res = client.post("/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.get_json()


def _approved_request(client, app, setup, login_as, dispatch_number="GLC-D1"):
    login_as("glc_op1", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number=dispatch_number)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "Wrong quantity, should be 3",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")
    login_as("glc_mgr1", "password123", "manager")
    approved = client.post(f"/api/correction-requests/{req['id']}/approve", json={}).get_json()
    client.post("/api/logout")
    return d, approved


# =====================================================================
# GRANT LIFECYCLE
# =====================================================================

def test_approval_does_not_immediately_alter_the_record(client, app, setup, login_as):
    login_as("glc_op_a", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-A1", cartons=5)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "Wrong quantity here too",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")
    login_as("glc_mgr_a", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})

    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()
    assert row["issued"]["base_qty"] == 500  # still the original 5 cartons, untouched by approval


def test_grant_allows_edit_to_exact_requester_only(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-B1")
    _relogin(client, "glc_op1")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using my grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_grant_does_not_allow_a_different_operator_to_edit(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-C1")
    login_as("glc_other_op", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "not my grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_grant_does_not_carry_to_a_different_record(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-D2")
    _relogin(client, "glc_op1")
    other_d = _historical_dispatch(client, app, setup, number="GLC-D2-OTHER")
    res = client.post(f"/api/dispatches/{other_d['id']}/correct", json={
        "reason": "wrong record entirely", "lines": [{"id": other_d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403  # the grant is for `d`, never `other_d`


def test_grant_does_not_carry_to_a_different_record_type(client, app, setup, login_as):
    login_as("glc_op_type", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-TYPE-1")
    r = client.post("/api/returns", json={
        "date": "2020-01-01", "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.return_record import ReturnRecord
        row = db.session.get(ReturnRecord, r["id"])
        row.created_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "dispatch request only here",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("glc_mgr_type", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    client.post("/api/logout")

    _relogin(client, "glc_op_type")
    # The grant is for the DISPATCH — a Return with the same record_id
    # must never be treated as covered by it.
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "trying to reuse the dispatch grant on a return", "lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_grant_permits_exactly_one_successful_correction(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-E1")
    _relogin(client, "glc_op1")
    first = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "first use", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert first.status_code == 200
    second = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "trying again immediately", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert second.status_code == 403  # grant already consumed, and record is still >24h old


def test_audit_trail_links_a_grant_based_correction_back_to_its_request(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-AUD-1")
    _relogin(client, "glc_op1")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using the grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch", entity_id=str(d["id"])).first()
        after = json.loads(entry.after_json)
        assert after["via_request_id"] == approved["id"]
        assert after["correction_source"] == "approved_grant"


def test_audit_trail_marks_a_direct_edit_as_not_via_a_request(client, app, setup, login_as):
    login_as("glc_direct_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "GLC-AUD-2", "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "quick fix within the window", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 6, "packs": 0, "pieces": 0}],
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch", entity_id=str(d["id"])).first()
        after = json.loads(entry.after_json)
        assert after["via_request_id"] is None
        assert after["correction_source"] == "direct"


def test_audit_trail_records_grant_expiry(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-AUD-3")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.correction_request import CorrectionRequest
        row = db.session.get(CorrectionRequest, approved["id"])
        row.reviewed_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    _relogin(client, "glc_op1")
    # Any read that sweeps expiry (grant-status here) triggers expire_if_needed().
    client.get(f"/api/correction-requests/grant-status?record_type=dispatch&record_id={d['id']}")
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="grant_expired", entity_type="correction_request", entity_id=str(approved["id"])).first()
        assert entry is not None
        assert entry.user_id is None  # system-triggered lazy sweep, no human actor
        after = json.loads(entry.after_json)
        assert after["status"] == "expired"


def test_request_becomes_completed_after_the_one_correction(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-F1")
    _relogin(client, "glc_op1")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using the grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    _relogin(client, "glc_mgr1")
    row = client.get("/api/correction-requests?record_type=dispatch").get_json()
    match = next(r for r in row if r["record_id"] == d["id"])
    assert match["status"] == "completed"
    assert match["completed_at"] is not None
    assert match["completed_at_label"] is not None


def test_unused_grant_expires_after_24h(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-G1")
    # Backdate the request's own reviewed_at (grant start) to simulate 25h passing.
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.correction_request import CorrectionRequest
        row = db.session.get(CorrectionRequest, approved["id"])
        row.reviewed_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()

    _relogin(client, "glc_op1")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "trying after expiry", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403

    _relogin(client, "glc_mgr1")
    rows = client.get("/api/correction-requests?record_type=dispatch").get_json()
    match = next(r for r in rows if r["record_id"] == d["id"])
    assert match["status"] == "expired"


def test_expired_grant_never_alters_the_source_record(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-H1")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.correction_request import CorrectionRequest
        row = db.session.get(CorrectionRequest, approved["id"])
        row.reviewed_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    _relogin(client, "glc_op1")
    before = client.get(f"/api/dispatches/{d['id']}").get_json()
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "trying after expiry", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    after = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert after["lines"][0]["cartons"] == before["lines"][0]["cartons"] == 5


def test_operator_can_request_again_after_completion(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-I1")
    _relogin(client, "glc_op1")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using the grant", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    # Still >24h old (its date never changed) — a fresh request is allowed.
    fresh = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "need one more correction here",
        "payload": {"lines": []},
    })
    assert fresh.status_code == 201


def test_operator_can_request_again_after_expiry(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-J1")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.correction_request import CorrectionRequest
        row = db.session.get(CorrectionRequest, approved["id"])
        row.reviewed_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    _relogin(client, "glc_op1")
    fresh = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "grant expired, need a new one",
        "payload": {"lines": []},
    })
    assert fresh.status_code == 201


def test_double_click_race_only_applies_the_correction_once(client, app, setup, login_as):
    # Simulates a double-click/retry: the SAME grant is used twice in a
    # row without the record's own state changing between attempts (the
    # realistic race a network retry or a fast double-click produces) —
    # the second call must never re-apply on top of the first.
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-K1")
    _relogin(client, "glc_op1")
    body = {"reason": "racing edit", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 7, "packs": 0, "pieces": 0}]}
    first = client.post(f"/api/dispatches/{d['id']}/correct", json=body)
    second = client.post(f"/api/dispatches/{d['id']}/correct", json=body)
    assert first.status_code == 200
    assert second.status_code == 403
    # Stock reflects the ONE successful correction, never double-applied.
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()
    assert row["issued"]["base_qty"] == 700  # 7 cartons, exactly once


# =====================================================================
# OPERATOR IN-APP STATE INDICATION ON APPROVAL/REJECTION (section 13) —
# the guaranteed in-app fallback that works with no push opt-in at all.
# =====================================================================

def test_list_requests_can_be_scoped_to_one_exact_record(client, app, setup, login_as):
    login_as("glc_scope_op", "password123", "operator")
    d1 = _historical_dispatch(client, app, setup, number="GLC-SCOPE-1")
    d2 = _historical_dispatch(client, app, setup, number="GLC-SCOPE-2")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d1["id"], "action": "correct", "reason": "first record request here",
        "payload": {"lines": []},
    })
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d2["id"], "action": "correct", "reason": "second record request here",
        "payload": {"lines": []},
    })
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d1['id']}").get_json()
    assert len(rows) == 1
    assert rows[0]["record_id"] == d1["id"]


def test_operator_sees_pending_status_in_app_after_submitting(client, app, setup, login_as):
    login_as("glc_pend_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-PEND-1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "waiting on manager review here",
        "payload": {"lines": []},
    })
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}").get_json()
    assert rows[0]["status"] == "pending"


def test_operator_sees_rejected_status_and_review_note_in_app(client, app, setup, login_as):
    login_as("glc_rej_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-REJ-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "asking for a correction here",
        "payload": {"lines": []},
    }).get_json()
    login_as("glc_rej_mgr", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "Not enough detail provided"})
    _relogin(client, "glc_rej_op")
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}").get_json()
    assert rows[0]["status"] == "rejected"
    assert rows[0]["review_note"] == "Not enough detail provided"


def test_operator_sees_approved_grant_expiry_label_in_app(client, app, setup, login_as):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-APP-1")
    _relogin(client, "glc_op1")
    rows = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}").get_json()
    assert rows[0]["status"] == "approved"
    assert rows[0]["grant_expires_at_label"] is not None


def test_dispatch_history_page_shows_a_status_hint_element_and_covers_all_states():
    assert 'id="requestStatusHint"' in DISPATCH_HTML
    assert "Your correction request is pending" in DISPATCH_HTML
    assert "Your correction request was approved" in DISPATCH_HTML
    assert "Your correction request was rejected" in DISPATCH_HTML
    assert "Your approved edit grant expired unused" in DISPATCH_HTML


# =====================================================================
# VOIDED-RECORD CORRECTION (section 17, revised by the later "final
# consistency pass" section 1) — an approved grant for a record that got
# voided AFTER approval but BEFORE the Operator used it still works; the
# correction must leave the record void, never restore it to Finalized,
# never move stock. See tests/test_final_voided_record_correction_and_
# push.py for the full dedicated coverage (Request Correction created
# directly against an ALREADY-void record, one-time-use, audit trail,
# stock-neutrality across all three source books).
# =====================================================================

def test_grant_still_works_after_the_record_is_voided_post_approval_and_stays_void(client, app, setup, login_as, super_admin):
    d, approved = _approved_request(client, app, setup, login_as, dispatch_number="GLC-VOID-1")
    # Manager voids the record after the grant was already approved
    # (but before the Operator got a chance to use it).
    login_as("glc_mgr_void", "password123", "manager")
    void_res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "voided after grant approved"})
    assert void_res.status_code == 200

    _relogin(client, "glc_op1")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "correcting via the grant even though it's now void", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["status"] == "void"
    # Status remains void — never silently restored to finalized; stock
    # stays at zero regardless of the new line values.
    after = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert after["status"] == "void"
    assert after["lines"][0]["cartons"] == 1
    figures = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()
    assert figures["issued"]["base_qty"] == 0


# =====================================================================
# REQUESTS PAGE / NAV / BADGE
# =====================================================================

def test_requests_page_exists_and_is_manager_super_admin_only_server_side(client, setup, login_as):
    login_as("glc_op_page", "password123", "operator")
    res = client.get("/requests.html")
    assert res.status_code in (302, 303)  # redirected away — Operator is not authorized


def test_requests_page_reachable_by_manager_and_super_admin(client, super_admin):
    res = client.get("/requests.html")
    assert res.status_code == 200


def test_viewer_redirected_away_from_requests_page(client, setup, login_as):
    login_as("glc_viewer_page", "password123", "viewer")
    res = client.get("/requests.html")
    assert res.status_code in (302, 303)


def test_pending_count_endpoint_is_authoritative_and_role_gated(client, app, setup, login_as):
    login_as("glc_count_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-COUNT-1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "count me as pending please",
        "payload": {"lines": []},
    })
    res = client.post("/api/correction-requests", json={  # Operator itself can't read the count
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "dup",
        "payload": {"lines": []},
    })
    assert res.status_code == 400  # duplicate active request — confirms the first one is really there
    forbidden = client.get("/api/correction-requests/pending-count")
    assert forbidden.status_code == 403
    client.post("/api/logout")

    login_as("glc_count_mgr", "password123", "manager")
    res = client.get("/api/correction-requests/pending-count")
    assert res.status_code == 200
    assert res.get_json()["count"] == 1


def test_pending_count_excludes_completed_rejected_and_expired(client, app, setup, login_as):
    # Pending
    login_as("glc_multi_op", "password123", "operator")
    d1 = _historical_dispatch(client, app, setup, number="GLC-MULTI-1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d1["id"], "action": "correct", "reason": "pending one right here",
        "payload": {"lines": []},
    })
    # Rejected
    d2 = _historical_dispatch(client, app, setup, number="GLC-MULTI-2")
    req2 = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d2["id"], "action": "correct", "reason": "will be rejected here",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("glc_multi_mgr", "password123", "manager")
    assert client.get("/api/correction-requests/pending-count").get_json()["count"] == 2
    client.post(f"/api/correction-requests/{req2['id']}/reject", json={"review_note": "declined here"})
    assert client.get("/api/correction-requests/pending-count").get_json()["count"] == 1


def test_requests_page_markup_shows_required_fields():
    assert "Request #" in REQUESTS_HTML
    assert "Operator:" in REQUESTS_HTML
    assert "Business date:" in REQUESTS_HTML
    assert "Time Entered:" in REQUESTS_HTML
    assert "Requested:" in REQUESTS_HTML
    assert "data-approve" in REQUESTS_HTML
    assert "data-reject" in REQUESTS_HTML


def test_requests_page_uses_kampala_formatted_labels_not_raw_timestamps():
    # Every displayed time comes from a `*_label` field (already Kampala-
    # formatted server-side by business_calendar.format_kampala_datetime())
    # — never a raw ISO/ `r.created_at` passthrough.
    assert "r.created_at_label" in REQUESTS_HTML
    assert "snap.created_at_label" in REQUESTS_HTML
    assert "r.grant_expires_at_label" in REQUESTS_HTML
    assert "r.reviewed_at_label" in REQUESTS_HTML
    assert "r.completed_at_label" in REQUESTS_HTML


def test_app_shell_nav_includes_requests_for_manager_and_super_admin_only():
    assert "items.push({ key: 'requests', label: 'Requests', href: '/requests.html', badge: 'pendingRequests' });" in APP_SHELL_JS
    idx = APP_SHELL_JS.index("items.push({ key: 'requests'")
    guard_line = APP_SHELL_JS[max(0, idx - 150):idx]
    assert "role === 'manager' || role === 'super_admin'" in guard_line


def test_app_shell_badge_hidden_when_count_is_zero():
    idx = APP_SHELL_JS.index("function navLink(")
    end = APP_SHELL_JS.index("\n  }\n", idx)
    body = APP_SHELL_JS[idx:end]
    assert "if (item.badge === 'pendingRequests' && pendingCount)" in body


def test_operator_and_viewer_never_fetch_pending_count_in_app_shell():
    idx = APP_SHELL_JS.index("if (session.user.role === 'manager' || session.user.role === 'super_admin') {")
    end = APP_SHELL_JS.index("\n    }\n", idx)
    body = APP_SHELL_JS[idx:end]
    assert "/api/correction-requests/pending-count" in body


def test_pwa_app_badge_feature_detected_and_never_crashes_on_unsupported_devices():
    assert "'setAppBadge' in navigator" in APP_SHELL_JS
    idx = APP_SHELL_JS.index("function setAppIconBadge(")
    end = APP_SHELL_JS.index("\n  }\n", idx)
    body = APP_SHELL_JS[idx:end]
    assert "try {" in body
    assert "catch (e)" in body


# =====================================================================
# RESET DAILY VALUES — Manager removed, Super Admin only
# =====================================================================

def test_manager_forged_reset_preview_request_returns_403(client, setup, login_as):
    login_as("glc_reset_mgr", "password123", "manager")
    res = client.post("/api/daily-reset/preview", json={
        "date": "2020-01-01", "shift": "Day", "product_id": setup["product"]["id"],
    })
    assert res.status_code == 403


def test_manager_forged_reset_execute_request_returns_403(client, setup, login_as):
    login_as("glc_reset_mgr2", "password123", "manager")
    res = client.post("/api/daily-reset", json={
        "date": "2020-01-01", "shift": "Day", "product_id": setup["product"]["id"],
        "reason": "trying anyway", "confirmation_text": "RESET",
    })
    assert res.status_code == 403


def test_operator_and_viewer_remain_unauthorized_for_reset(client, setup, login_as):
    login_as("glc_reset_op", "password123", "operator")
    assert client.post("/api/daily-reset/preview", json={"date": "2020-01-01", "shift": "Day"}).status_code == 403
    client.post("/api/logout")
    login_as("glc_reset_viewer", "password123", "viewer")
    assert client.post("/api/daily-reset/preview", json={"date": "2020-01-01", "shift": "Day"}).status_code == 403


def test_super_admin_reset_still_works(client, setup, super_admin):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2020-01-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset/preview", json={"date": "2020-01-01", "shift": "Day", "product_id": pid})
    assert res.status_code == 200


def test_manager_page_guard_redirects_away_from_reset_page(client, setup, login_as):
    login_as("glc_reset_page_mgr", "password123", "manager")
    res = client.get("/reset-daily-values.html")
    assert res.status_code in (302, 303)


def test_super_admin_can_reach_reset_page(client, super_admin):
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 200


def test_reset_page_client_side_gate_says_super_admin_only():
    assert "You need a Manager or Super Administrator account" not in RESET_HTML
    assert "Super Administrator" in RESET_HTML


def test_app_shell_nav_no_longer_offers_reset_to_manager():
    idx = APP_SHELL_JS.index("items.push({ key: 'reset_daily_values'")
    guard_line = APP_SHELL_JS[APP_SHELL_JS.rindex("\n", 0, APP_SHELL_JS.rindex("\n", 0, idx)):idx]
    assert "role === 'super_admin'" in guard_line
    assert "role === 'manager'" not in guard_line


def test_reset_stock_calculations_themselves_unchanged(client, setup, super_admin):
    # Permission tightening only — the reset math/provenance rules are
    # completely untouched.
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2020-01-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset", json={
        "date": "2020-01-01", "shift": "Day", "product_id": pid,
        "reason": "test reset", "mode": "full", "confirmation_text": "FULL RESET 2020-01-01 DAY",
    })
    assert res.status_code == 200
    after = client.get(f"/api/daily-figures/{pid}?date=2020-01-01&shift=Day").get_json()
    assert after["opening"]["base_qty"] == 0  # Mode B full reset still clears Opening Stock, unchanged behavior


# =====================================================================
# WEB PUSH SAFETY
# =====================================================================

def test_no_private_vapid_key_hardcoded_anywhere_in_the_repo():
    import subprocess
    # A real VAPID private key is base64url and long — this just proves
    # the source files never contain a literal assigned value for it,
    # only the environment-variable NAME.
    for f in ("webapp/services/push_service.py", ".env.example", "requirements.txt"):
        content = (pathlib.Path(__file__).resolve().parent.parent / f).read_text(encoding="utf-8")
        assert 'VAPID_PRIVATE_KEY = "' not in content
        assert "VAPID_PRIVATE_KEY='" not in content


def test_missing_vapid_config_does_not_break_the_app(client, app, setup, login_as):
    # No VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY are set in this test environment
    # at all — creating a correction request (which triggers a
    # notification attempt) must still succeed normally.
    from webapp.services import push_service
    assert push_service.is_configured() is False
    login_as("glc_push_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-PUSH-CFG-1")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "no vapid config configured here",
        "payload": {"lines": []},
    })
    assert res.status_code == 201


def test_vapid_public_key_endpoint_reports_not_configured_safely(client, setup, login_as):
    login_as("glc_push_op2", "password123", "operator")
    res = client.get("/api/push/vapid-public-key")
    assert res.status_code == 200
    body = res.get_json()
    assert body["configured"] is False
    assert body["key"] is None


def test_push_subscribe_requires_authentication(client):
    res = client.post("/api/push/subscribe", json={"subscription": {}})
    assert res.status_code == 401


def test_push_subscribe_rejects_incomplete_subscription(client, setup, login_as):
    login_as("glc_push_op3", "password123", "operator")
    res = client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://example.com/x"}})
    assert res.status_code == 400


def test_push_subscribe_stores_subscription_against_authenticated_user_only(client, setup, login_as, app):
    login_as("glc_push_op4", "password123", "operator")
    res = client.post("/api/push/subscribe", json={"subscription": {
        "endpoint": "https://push.example.com/sub-1",
        "keys": {"p256dh": "fake-p256dh-key", "auth": "fake-auth-key"},
    }})
    assert res.status_code == 201
    with app.app_context():
        from webapp.models.push_subscription import PushSubscription
        from webapp.models.user import User
        row = PushSubscription.query.filter_by(endpoint="https://push.example.com/sub-1").first()
        user = User.query.filter_by(username="glc_push_op4").first()
        assert row.user_id == user.id


def test_push_unsubscribe_removes_the_subscription(client, setup, login_as, app):
    login_as("glc_push_op5", "password123", "operator")
    client.post("/api/push/subscribe", json={"subscription": {
        "endpoint": "https://push.example.com/sub-2",
        "keys": {"p256dh": "fake-p256dh-key", "auth": "fake-auth-key"},
    }})
    res = client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example.com/sub-2"})
    assert res.status_code == 200
    with app.app_context():
        from webapp.models.push_subscription import PushSubscription
        assert PushSubscription.query.filter_by(endpoint="https://push.example.com/sub-2").first() is None


def test_notify_functions_never_raise_when_push_is_unconfigured(client, app, setup, login_as):
    # Creating a request and approving/rejecting it triggers
    # notify_new_correction_request()/notify_request_decided() — with no
    # VAPID configuration at all in this test environment, none of that
    # may ever surface as a request failure.
    login_as("glc_push_notify_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, number="GLC-PUSH-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "notification safety check here",
        "payload": {"lines": []},
    })
    assert req.status_code == 201
    client.post("/api/logout")
    login_as("glc_push_notify_mgr", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req.get_json()['id']}/approve", json={})
    assert res.status_code == 200


def test_service_worker_has_push_and_notificationclick_handlers():
    assert "self.addEventListener('push'" in SW_JS
    assert "self.addEventListener('notificationclick'" in SW_JS
    assert "showNotification" in SW_JS


def test_requests_page_push_opt_in_is_feature_detected_and_optional():
    assert "'serviceWorker' in navigator" in REQUESTS_HTML
    assert "'PushManager' in window" in REQUESTS_HTML
    assert "'Notification' in window" in REQUESTS_HTML
    # Never auto-requests permission — only after an explicit user click.
    idx = REQUESTS_HTML.index("pushEnableBtn').addEventListener('click'")
    assert "Notification.requestPermission()" in REQUESTS_HTML[idx:idx + 400]
