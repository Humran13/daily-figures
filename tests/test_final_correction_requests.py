"""
Full targeted Operator correction/void/requests/notification package —
historical correction request workflow.

Once an Operator's direct 24-hour Edit window (record.created_at + 24h;
see test_final_operator_same_day_edit_window.py) has closed, they submit
a CorrectionRequest (action=correct only — Void has no request path at
all, see test_final_void_workflow.py), which:
  - never mutates the underlying record on creation,
  - is visible to Manager/Super Admin (and, read-only, to the requesting
    Operator for their own requests) for review,
  - on approval, starts a one-time, record-specific, 24-hour edit GRANT
    for the requesting Operator — it does NOT immediately apply anything
    (see test_final_grant_lifecycle.py for the grant's own full
    consume/expire coverage),
  - on rejection, changes nothing on the underlying record at all,
  - is fully audited (create/approve/reject all produce AuditLog rows).

This is a single approval queue in front of existing, already-audited
mutation functions — never a second/competing correction engine.
"""
import datetime
import json

import pytest

from webapp.services.business_calendar import utcnow


def _make_product(client, name="CR Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("cr_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "CR Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "CR Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _backdate_dispatch(app, dispatch_id, hours=25):
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        row = db.session.get(Dispatch, dispatch_id)
        row.created_at = utcnow() - datetime.timedelta(hours=hours)
        db.session.commit()


def _historical_dispatch(client, app, setup, dispatch_number="CR-D1", cartons=5):
    """A finalized dispatch whose 24-hour direct Edit window has already
    closed — backdates created_at directly (never the business `date`,
    which is a separate, independent field under the new rule)."""
    d = client.post("/api/dispatches", json={
        "dispatch_number": dispatch_number, "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    _backdate_dispatch(app, d["id"])
    return d


def _issued_base_qty(client, product_id, date="2020-01-01"):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift=Day").get_json()
    return row["issued"]["base_qty"]


# =====================================================================
# CREATE
# =====================================================================

def test_operator_can_create_correction_request_for_own_historical_dispatch(client, app, setup, login_as):
    login_as("cr_op1", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "Wrong quantity, should be 3 not 5",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    })
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "pending"
    assert body["record_type"] == "dispatch"
    assert body["record_id"] == d["id"]
    assert body["requested_by_username"] == "cr_op1"


def test_reason_is_required(client, app, setup, login_as):
    login_as("cr_op_noreason", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    })
    assert res.status_code == 400


def test_reason_must_be_meaningfully_descriptive(client, app, setup, login_as):
    login_as("cr_op_shortreason", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "oops",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    })
    assert res.status_code == 400


def test_creating_a_correction_request_does_not_alter_stock_immediately(client, app, setup, login_as):
    login_as("cr_op2", "password123", "operator")
    d = _historical_dispatch(client, app, setup, cartons=5)
    before = _issued_base_qty(client, setup["product"]["id"])
    assert before == 500

    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity entered here",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}]},
    })
    after = _issued_base_qty(client, setup["product"]["id"])
    assert after == before == 500  # unchanged until approved AND used


def test_operator_cannot_request_for_a_record_they_do_not_own(client, app, setup, super_admin, login_as):
    d = _historical_dispatch(client, app, setup)  # created while logged in as super_admin
    login_as("cr_notowner", "password123", "operator")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "not mine to request",
        "payload": {"lines": []},
    })
    assert res.status_code == 403


def test_request_creation_rejected_while_still_within_the_24h_window(client, setup, login_as):
    # A record still within its direct Edit window should use direct Edit
    # instead — the two paths must never overlap.
    login_as("cr_fresh_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "CR-FRESH-1", "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "trying anyway too early",
        "payload": {"lines": []},
    })
    assert res.status_code == 409


def test_void_action_is_refused_no_request_void_path_exists(client, app, setup, login_as):
    login_as("cr_void_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "trying to request a void",
    })
    assert res.status_code == 400
    assert "no request needed" in res.get_json()["error"].lower()


def test_viewer_cannot_create_a_correction_request(client, app, setup, super_admin, login_as):
    d = _historical_dispatch(client, app, setup)
    login_as("cr_viewer", "password123", "viewer")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "x", "payload": {"lines": []},
    })
    assert res.status_code == 403


def test_manager_cannot_create_a_correction_request(client, app, setup, super_admin, login_as):
    # Manager/Super Admin never need this — they correct/void directly.
    d = _historical_dispatch(client, app, setup)
    login_as("cr_mgr_create", "password123", "manager")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "x", "payload": {"lines": []},
    })
    assert res.status_code == 403


def test_correct_request_requires_a_payload(client, app, setup, login_as):
    login_as("cr_op_nopayload", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a payload though",
    })
    assert res.status_code == 400


def test_duplicate_active_request_is_rejected_with_a_clear_message(client, app, setup, login_as):
    login_as("cr_dup_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup)
    first = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "first request submitted here",
        "payload": {"lines": []},
    })
    assert first.status_code == 201
    second = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "second request submitted here",
        "payload": {"lines": []},
    })
    assert second.status_code == 400
    assert "already" in second.get_json()["error"].lower()


# =====================================================================
# LIST — visibility scoping
# =====================================================================

def test_operator_sees_only_their_own_requests(client, app, setup, login_as):
    login_as("cr_op_a", "password123", "operator")
    d1 = _historical_dispatch(client, app, setup, dispatch_number="CR-A1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d1["id"], "action": "correct", "reason": "a's request submitted",
        "payload": {"lines": []},
    })
    client.post("/api/logout")

    login_as("cr_op_b", "password123", "operator")
    d2 = _historical_dispatch(client, app, setup, dispatch_number="CR-B1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d2["id"], "action": "correct", "reason": "b's request submitted",
        "payload": {"lines": []},
    })

    rows = client.get("/api/correction-requests").get_json()
    assert all(r["requested_by_username"] == "cr_op_b" for r in rows)
    assert len(rows) == 1


def test_manager_sees_every_request(client, app, setup, login_as):
    login_as("cr_op_c", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-C1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "c's request submitted",
        "payload": {"lines": []},
    })
    client.post("/api/logout")

    login_as("cr_mgr_list", "password123", "manager")
    rows = client.get("/api/correction-requests").get_json()
    assert any(r["requested_by_username"] == "cr_op_c" for r in rows)


def test_viewer_cannot_list_correction_requests(client, app, setup, super_admin, login_as):
    login_as("cr_viewer2", "password123", "viewer")
    res = client.get("/api/correction-requests")
    assert res.status_code == 403


# =====================================================================
# APPROVE — starts a grant, never applies anything immediately
# =====================================================================

def test_manager_can_approve_a_correction_request_without_altering_the_record(client, app, setup, login_as):
    login_as("cr_op_d", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-D1", cartons=5)
    before = _issued_base_qty(client, setup["product"]["id"])
    assert before == 500

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity entered here",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")

    login_as("cr_mgr_approve", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "checked, approved"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "approved"
    assert body["reviewed_by_username"] == "cr_mgr_approve"
    assert body["grant_expires_at"] is not None

    # The underlying record is COMPLETELY untouched by approval alone.
    after = _issued_base_qty(client, setup["product"]["id"])
    assert after == before == 500
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["lines"][0]["cartons"] == 5


def test_double_approving_the_same_request_is_refused(client, app, setup, login_as):
    login_as("cr_op_f", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-F1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_f", "password123", "manager")
    first = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert first.status_code == 200
    second = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert second.status_code == 400


def test_super_admin_can_approve_requests_too(client, app, setup, login_as, super_admin):
    login_as("cr_op_i", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-I1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    client.post("/api/login", json={"username": "cr_root", "password": "password123"})
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 200


def test_operator_cannot_approve_any_request(client, app, setup, login_as):
    login_as("cr_op_j", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-J1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 403


def test_viewer_cannot_approve_any_request(client, app, setup, super_admin, login_as):
    login_as("cr_op_k", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-K1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_viewer3", "password123", "viewer")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 403


# =====================================================================
# REJECT
# =====================================================================

def test_manager_can_reject_a_request_leaving_record_unchanged(client, app, setup, login_as):
    login_as("cr_op_l", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-L1", cartons=4)
    before_issued = _issued_base_qty(client, setup["product"]["id"])

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "trying to correct this one",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("cr_mgr_l", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "not a valid reason"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "rejected"

    after_issued = _issued_base_qty(client, setup["product"]["id"])
    assert after_issued == before_issued == 400


def test_reject_requires_a_review_note(client, app, setup, login_as):
    login_as("cr_op_m", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-M1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_m", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={})
    assert res.status_code == 400


def test_operator_cannot_reject_any_request(client, app, setup, login_as):
    login_as("cr_op_n", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-N1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "needs a fix here please",
        "payload": {"lines": []},
    }).get_json()
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "no"})
    assert res.status_code == 403


# =====================================================================
# AUDIT — create/approve/reject each produce a correction_request entry
# =====================================================================

def test_full_audit_trail_for_create_approve(client, app, setup, login_as):
    login_as("cr_op_o", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-O1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "audit trail check please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_o", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "ok"})

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        create_entry = AuditLog.query.filter_by(action="create", entity_type="correction_request", entity_id=str(req["id"])).first()
        approve_entry = AuditLog.query.filter_by(action="approve", entity_type="correction_request", entity_id=str(req["id"])).first()
        assert create_entry is not None
        assert approve_entry is not None
        assert create_entry.username == "cr_op_o"
        assert approve_entry.username == "cr_mgr_o"


def test_full_audit_trail_for_create_reject(client, app, setup, login_as):
    login_as("cr_op_p", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="CR-P1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "audit trail check please",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_p", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "declined"})

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        reject_entry = AuditLog.query.filter_by(action="reject", entity_type="correction_request", entity_id=str(req["id"])).first()
        assert reject_entry is not None
        assert reject_entry.username == "cr_mgr_p"


# =====================================================================
# Same shape applies to Returns/Production
# =====================================================================

def test_returns_correction_request_full_cycle(client, app, setup, login_as):
    login_as("cr_rop1", "password123", "operator")
    r = client.post("/api/returns", json={
        "date": "2020-01-01", "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.return_record import ReturnRecord
        row = db.session.get(ReturnRecord, r["id"])
        row.created_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()["return_"]["base_qty"] == 300

    req = client.post("/api/correction-requests", json={
        "record_type": "returns", "record_id": r["id"], "action": "correct", "reason": "wrong recipient entered here",
        "payload": {"lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    })
    assert req.status_code == 201
    req_id = req.get_json()["id"]
    client.post("/api/logout")

    login_as("cr_rmgr1", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req_id}/approve", json={})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"
    # Still untouched — approval only grants, never applies.
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()["return_"]["base_qty"] == 300


def test_production_correction_request_full_cycle(client, app, setup, login_as):
    login_as("cr_pop1", "password123", "operator")
    p = client.post("/api/production", json={
        "date": "2020-01-01", "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.production_record import ProductionRecord
        row = db.session.get(ProductionRecord, p["id"])
        row.created_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()["production"]["base_qty"] == 400

    req = client.post("/api/correction-requests", json={
        "record_type": "production", "record_id": p["id"], "action": "correct", "reason": "wrong quantity produced here",
        "payload": {"lines": [{"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}]},
    })
    assert req.status_code == 201
    req_id = req.get_json()["id"]
    client.post("/api/logout")

    login_as("cr_pmgr1", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req_id}/approve", json={})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"
