"""
Round D, Part 1D — Historical correction/void request workflow.

Once a record's own business date is no longer today (Africa/Kampala),
an Operator can no longer directly Edit/Void it (see
test_final_operator_same_day_edit_window.py) — instead they submit a
CorrectionRequest, which:
  - never mutates the underlying record on creation,
  - is visible to Manager/Super Admin (and, read-only, to the requesting
    Operator for their own requests) for review,
  - on approval, invokes the EXACT SAME correct_record()/void_*()
    functions Manager/Super Admin already use directly, with the
    reviewer (not the requester) as the actor of record,
  - on rejection, changes nothing on the underlying record at all,
  - is fully audited (create/approve/reject all produce AuditLog rows).

This is a single approval queue in front of existing, already-audited
mutation functions — never a second/competing correction engine.
"""
import json

import pytest

from webapp.services.business_calendar import business_today, is_same_business_day


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


def _yesterday():
    import datetime
    return (datetime.date.fromisoformat(business_today()) - datetime.timedelta(days=1)).isoformat()


def _historical_dispatch(client, setup, dispatch_number="CR-D1", cartons=5):
    d = client.post("/api/dispatches", json={
        "dispatch_number": dispatch_number, "date": _yesterday(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


def _issued_base_qty(client, product_id, date):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift=Day").get_json()
    return row["issued"]["base_qty"]


# =====================================================================
# CREATE
# =====================================================================

def test_operator_can_create_correction_request_for_own_historical_dispatch(client, setup, login_as):
    login_as("cr_op1", "password123", "operator")
    d = _historical_dispatch(client, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    })
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "pending"
    assert body["record_type"] == "dispatch"
    assert body["record_id"] == d["id"]
    assert body["requested_by_username"] == "cr_op1"


def test_creating_a_correction_request_does_not_alter_stock_immediately(client, setup, login_as):
    login_as("cr_op2", "password123", "operator")
    d = _historical_dispatch(client, setup, cartons=5)
    before = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert before == 500

    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}]},
    })
    after = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert after == before == 500  # unchanged until approved


def test_creating_a_void_request_does_not_alter_stock_immediately(client, setup, login_as):
    login_as("cr_op3", "password123", "operator")
    d = _historical_dispatch(client, setup, cartons=5)
    before = _issued_base_qty(client, setup["product"]["id"], _yesterday())

    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "entered in error",
    })
    after = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert after == before == 500
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "finalized"  # not void yet


def test_operator_cannot_request_for_a_record_they_do_not_own(client, setup, super_admin, login_as):
    d = _historical_dispatch(client, setup)  # created while logged in as super_admin
    login_as("cr_notowner", "password123", "operator")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "not mine",
    })
    assert res.status_code == 403


def test_request_creation_rejected_for_a_still_same_day_record(client, setup, login_as):
    # Same-day records use the direct Edit/Void action instead — the two
    # paths must never overlap.
    login_as("cr_sameday_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "CR-SAMEDAY-1", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "trying anyway",
    })
    assert res.status_code == 409


def test_viewer_cannot_create_a_correction_request(client, setup, super_admin, login_as):
    d = _historical_dispatch(client, setup)
    login_as("cr_viewer", "password123", "viewer")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    })
    assert res.status_code == 403


def test_manager_cannot_create_a_correction_request(client, setup, super_admin, login_as):
    # Manager/Super Admin never need this — they correct/void directly.
    d = _historical_dispatch(client, setup)
    login_as("cr_mgr_create", "password123", "manager")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    })
    assert res.status_code == 403


def test_correct_request_requires_a_payload(client, setup, login_as):
    login_as("cr_op_nopayload", "password123", "operator")
    d = _historical_dispatch(client, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "x",
    })
    assert res.status_code == 400


# =====================================================================
# LIST — visibility scoping
# =====================================================================

def test_operator_sees_only_their_own_requests(client, setup, login_as):
    login_as("cr_op_a", "password123", "operator")
    d1 = _historical_dispatch(client, setup, dispatch_number="CR-A1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d1["id"], "action": "void", "reason": "a's request",
    })
    client.post("/api/logout")

    login_as("cr_op_b", "password123", "operator")
    d2 = _historical_dispatch(client, setup, dispatch_number="CR-B1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d2["id"], "action": "void", "reason": "b's request",
    })

    rows = client.get("/api/correction-requests").get_json()
    assert all(r["requested_by_username"] == "cr_op_b" for r in rows)
    assert len(rows) == 1


def test_manager_sees_every_request(client, setup, login_as):
    login_as("cr_op_c", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-C1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "c's request",
    })
    client.post("/api/logout")

    login_as("cr_mgr_list", "password123", "manager")
    rows = client.get("/api/correction-requests").get_json()
    assert any(r["requested_by_username"] == "cr_op_c" for r in rows)


def test_viewer_cannot_list_correction_requests(client, setup, super_admin, login_as):
    login_as("cr_viewer2", "password123", "viewer")
    res = client.get("/api/correction-requests")
    assert res.status_code == 403


# =====================================================================
# APPROVE — correction
# =====================================================================

def test_manager_can_approve_a_correction_request_applying_it_exactly_once(client, setup, login_as):
    login_as("cr_op_d", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-D1", cartons=5)
    before = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert before == 500

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")

    login_as("cr_mgr_approve", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "checked, approved"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"
    assert res.get_json()["reviewed_by_username"] == "cr_mgr_approve"

    after = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert after == 300  # applied exactly once, new quantity


def test_approving_a_correction_reuses_correct_record_actor_is_reviewer(client, setup, login_as, app):
    login_as("cr_op_e", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-E1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "wrong quantity",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_e", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch", entity_id=str(d["id"])).first()
        assert entry is not None
        after = json.loads(entry.after_json)
        assert after["actor_role"] == "manager"  # reviewer, not the requesting operator


def test_double_approving_the_same_request_is_refused(client, setup, login_as):
    login_as("cr_op_f", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-F1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_f", "password123", "manager")
    first = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert first.status_code == 200
    second = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert second.status_code == 400  # already reviewed — refused, not silently reapplied


# =====================================================================
# APPROVE — void
# =====================================================================

def test_manager_can_approve_a_void_request_removing_stock_effect_exactly_once(client, setup, login_as):
    login_as("cr_op_g", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-G1", cartons=6)
    before = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert before == 600

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "duplicate entry",
    }).get_json()
    client.post("/api/logout")

    login_as("cr_mgr_g", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 200

    after = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert after == 0
    updated = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert updated["status"] == "void"
    assert updated["void_reason"] == "duplicate entry"


def test_voided_record_still_appears_in_history_after_request_approved(client, setup, login_as):
    login_as("cr_op_h", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-H1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_h", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})

    listed = client.get("/api/dispatches?limit=200").get_json()["results"]
    assert any(r["id"] == d["id"] and r["status"] == "void" for r in listed)


def test_super_admin_can_approve_requests_too(client, setup, login_as, super_admin):
    login_as("cr_op_i", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-I1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    client.post("/api/logout")

    client.post("/api/login", json={"username": "cr_root", "password": "password123"})
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 200


def test_operator_cannot_approve_any_request(client, setup, login_as):
    login_as("cr_op_j", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-J1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 403


def test_viewer_cannot_approve_any_request(client, setup, super_admin, login_as):
    login_as("cr_op_k", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-K1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    client.post("/api/logout")
    login_as("cr_viewer3", "password123", "viewer")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 403


# =====================================================================
# REJECT
# =====================================================================

def test_manager_can_reject_a_request_leaving_record_unchanged(client, setup, login_as):
    login_as("cr_op_l", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-L1", cartons=4)
    before_status = client.get(f"/api/dispatches/{d['id']}").get_json()["status"]
    before_issued = _issued_base_qty(client, setup["product"]["id"], _yesterday())

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "trying to void",
    }).get_json()
    client.post("/api/logout")

    login_as("cr_mgr_l", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "not a valid reason"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "rejected"

    after_status = client.get(f"/api/dispatches/{d['id']}").get_json()["status"]
    after_issued = _issued_base_qty(client, setup["product"]["id"], _yesterday())
    assert after_status == before_status == "finalized"
    assert after_issued == before_issued == 400


def test_reject_requires_a_review_note(client, setup, login_as):
    login_as("cr_op_m", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-M1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    client.post("/api/logout")
    login_as("cr_mgr_m", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={})
    assert res.status_code == 400


def test_operator_cannot_reject_any_request(client, setup, login_as):
    login_as("cr_op_n", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-N1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    }).get_json()
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "no"})
    assert res.status_code == 403


# =====================================================================
# AUDIT — create/approve/reject each produce a correction_request entry
# =====================================================================

def test_full_audit_trail_for_create_approve(client, setup, login_as, app):
    login_as("cr_op_o", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-O1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "audit trail check",
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


def test_full_audit_trail_for_create_reject(client, setup, login_as, app):
    login_as("cr_op_p", "password123", "operator")
    d = _historical_dispatch(client, setup, dispatch_number="CR-P1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "audit trail check",
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

def test_returns_void_request_full_cycle(client, setup, login_as):
    login_as("cr_rop1", "password123", "operator")
    r = client.post("/api/returns", json={
        "date": _yesterday(), "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={_yesterday()}&shift=Day").get_json()["return_"]["base_qty"] == 300

    req = client.post("/api/correction-requests", json={
        "record_type": "returns", "record_id": r["id"], "action": "void", "reason": "duplicate return",
    })
    assert req.status_code == 201
    req_id = req.get_json()["id"]
    client.post("/api/logout")

    login_as("cr_rmgr1", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req_id}/approve", json={})
    assert res.status_code == 200
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "void"
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={_yesterday()}&shift=Day").get_json()["return_"]["base_qty"] == 0


def test_production_void_request_full_cycle(client, setup, login_as):
    login_as("cr_pop1", "password123", "operator")
    p = client.post("/api/production", json={
        "date": _yesterday(), "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={_yesterday()}&shift=Day").get_json()["production"]["base_qty"] == 400

    req = client.post("/api/correction-requests", json={
        "record_type": "production", "record_id": p["id"], "action": "void", "reason": "duplicate production",
    })
    assert req.status_code == 201
    req_id = req.get_json()["id"]
    client.post("/api/logout")

    login_as("cr_pmgr1", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req_id}/approve", json={})
    assert res.status_code == 200
    assert client.get(f"/api/production/{p['id']}").get_json()["status"] == "void"
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={_yesterday()}&shift=Day").get_json()["production"]["base_qty"] == 0
