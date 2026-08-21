"""
Final consistency pass — three targeted corrections on top of the
already-complete Full targeted Operator correction/void/requests/
notification package:

  1. An Operator's OWN voided Dispatch/Return/Production can now get a
     Request Correction (previously impossible by construction — that
     was wrong; see record_correction_service.correct_record()'s new
     void-preserving handling). The approved one-time grant may edit the
     record's details, but it must NEVER unvoid it, restore stock
     contribution, or create any compensating movement.
  2. `pywebpush` is a real, active requirements.txt dependency — a
     normal Docker build already installs it, no manual step needed.
  3. Background Web Push: the service worker's own `push` handler can
     update the platform's app-icon badge (where supported) even while
     the PWA is fully closed, using an authoritative count carried in
     the push payload itself.
"""
import json
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
SW_JS = (STATIC / "sw.js").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC / "production.html").read_text(encoding="utf-8")
REQUIREMENTS_TXT = (pathlib.Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")


def _make_product(client, name="VRC Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("vrc_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "VRC Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "VRC Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _relogin(client, username, password="password123"):
    client.post("/api/logout")
    res = client.post("/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.get_json()


def _void_dispatch(client, setup, number="VRC-D1", cartons=5, reason="customer cancelled"):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": reason})
    assert res.status_code == 200, res.get_json()
    return d


def _void_return(client, setup, cartons=2, reason="wrong recipient"):
    r = client.post("/api/returns", json={
        "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": reason})
    assert res.status_code == 200, res.get_json()
    return r


def _void_production(client, setup, cartons=4, reason="entered under wrong shift"):
    p = client.post("/api/production", json={
        "date": "2020-01-01", "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": reason})
    assert res.status_code == 200, res.get_json()
    return p


def _daily_figure(client, setup):
    return client.get(f"/api/daily-figures/{setup['product']['id']}?date=2020-01-01&shift=Day").get_json()


# =====================================================================
# 1-3: Operator can Request Correction for own voided Dispatch/Return/Production
# =====================================================================

def test_operator_can_request_correction_for_own_voided_dispatch(client, setup, login_as):
    login_as("vrc_op1", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-REQ-1")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Wrong quantity was recorded before this was voided",
        "payload": {"lines": []},
    })
    assert res.status_code == 201
    assert res.get_json()["status"] == "pending"


def test_operator_can_request_correction_for_own_voided_return(client, setup, login_as):
    login_as("vrc_op2", "password123", "operator")
    r = _void_return(client, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "returns", "record_id": r["id"], "action": "correct",
        "reason": "Wrong product recorded before this was voided",
        "payload": {"lines": []},
    })
    assert res.status_code == 201
    assert res.get_json()["status"] == "pending"


def test_operator_can_request_correction_for_own_voided_production(client, setup, login_as):
    login_as("vrc_op3", "password123", "operator")
    p = _void_production(client, setup)
    res = client.post("/api/correction-requests", json={
        "record_type": "production", "record_id": p["id"], "action": "correct",
        "reason": "Wrong quantity recorded before this was voided",
        "payload": {"lines": []},
    })
    assert res.status_code == 201
    assert res.get_json()["status"] == "pending"


# =====================================================================
# 4: Another Operator cannot request correction
# =====================================================================

def test_another_operator_cannot_request_correction_for_a_voided_dispatch_they_do_not_own(client, setup, login_as):
    login_as("vrc_owner", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-OWN-1")
    client.post("/api/logout")
    login_as("vrc_other", "password123", "operator")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Trying to request a correction I do not own",
        "payload": {"lines": []},
    })
    assert res.status_code == 403


# =====================================================================
# 5-6: Manager / Super Admin can approve
# =====================================================================

def test_manager_can_approve_a_voided_record_correction_request(client, setup, login_as):
    login_as("vrc_op4", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-MGR-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Wrong quantity, needs correcting while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr1", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"


def test_super_admin_can_approve_a_voided_record_correction_request(client, setup, login_as, super_admin):
    login_as("vrc_op5", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-SA-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Wrong quantity, needs correcting while still void",
        "payload": {"lines": []},
    }).get_json()
    _relogin(client, "vrc_root")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"


# =====================================================================
# 7-13: approval/use never unvoids, never moves stock
# =====================================================================

def test_approval_does_not_unvoid_the_record(client, setup, login_as):
    login_as("vrc_op6", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-NOUNVOID-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Needs a correction while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr2", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["status"] == "void"


def test_approved_one_use_edit_works_on_a_voided_dispatch(client, setup, login_as):
    login_as("vrc_op7", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-EDIT-1", cartons=5)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Wrong quantity was recorded, should be 3 cartons",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr3", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op7")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "Correcting quantity while void", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0},
        ],
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["dispatch"]["status"] == "void"
    assert body["dispatch"]["lines"][0]["cartons"] == 3


def test_corrected_dispatch_remains_void_after_the_edit(client, setup, login_as):
    login_as("vrc_op8", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-STAY-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Needs a correction while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr4", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op8")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "Correcting recipient while void", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0},
        ],
    })
    after = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert after["status"] == "void"
    assert after["voided_by"] is not None
    assert after["voided_at"] is not None


def test_voided_dispatch_correction_leaves_issued_at_zero(client, setup, login_as):
    login_as("vrc_op9", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-ISSUED-1", cartons=5)
    before = _daily_figure(client, setup)
    assert before["issued"]["base_qty"] == 0
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Bumping the quantity way up while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr5", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op9")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "Big quantity change while void", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 500, "packs": 0, "pieces": 0},
        ],
    })
    after = _daily_figure(client, setup)
    assert after["issued"]["base_qty"] == 0  # still zero — void never contributes, no matter the line values


def test_voided_return_correction_leaves_returns_at_zero(client, setup, login_as):
    login_as("vrc_op10", "password123", "operator")
    r = _void_return(client, setup, cartons=2)
    req = client.post("/api/correction-requests", json={
        "record_type": "returns", "record_id": r["id"], "action": "correct",
        "reason": "Bumping the quantity while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr6", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op10")
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "Quantity change while void", "lines": [
            {"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 200, "packs": 0, "pieces": 0},
        ],
    })
    assert res.status_code == 200
    assert res.get_json()["return"]["status"] == "void"
    after = _daily_figure(client, setup)
    assert after["return_"]["base_qty"] == 0


def test_voided_production_correction_leaves_production_at_zero(client, setup, login_as):
    login_as("vrc_op11", "password123", "operator")
    p = _void_production(client, setup, cartons=4)
    req = client.post("/api/correction-requests", json={
        "record_type": "production", "record_id": p["id"], "action": "correct",
        "reason": "Bumping the quantity while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr7", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op11")
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "Quantity change while void", "lines": [
            {"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 300, "packs": 0, "pieces": 0},
        ],
    })
    assert res.status_code == 200
    assert res.get_json()["production"]["status"] == "void"
    after = _daily_figure(client, setup)
    assert after["production"]["base_qty"] == 0


def test_closing_and_opening_stock_unaffected_by_editing_a_voided_record(client, setup, login_as):
    login_as("vrc_op12", "password123", "operator")
    before = _daily_figure(client, setup)
    d = _void_dispatch(client, setup, number="VRC-D-CLOSING-1", cartons=5)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Changing quantity a lot while still void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr8", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op12")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "Large quantity change while void", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 999, "packs": 0, "pieces": 0},
        ],
    })
    after = _daily_figure(client, setup)
    assert after["opening"]["base_qty"] == before["opening"]["base_qty"]
    assert after["closing"]["base_qty"] == before["closing"]["base_qty"] == after["opening"]["base_qty"]


# =====================================================================
# 14: grant consumed exactly once
# =====================================================================

def test_grant_on_a_voided_record_is_consumed_exactly_once(client, setup, login_as):
    login_as("vrc_op13", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-ONCE-1", cartons=5)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Needs a one-time correction while void",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr9", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op13")
    body = {"reason": "first use", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]}
    first = client.post(f"/api/dispatches/{d['id']}/correct", json=body)
    second = client.post(f"/api/dispatches/{d['id']}/correct", json=body)
    assert first.status_code == 200
    assert second.status_code == 403
    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["status"] == "void"


def test_direct_edit_on_a_voided_record_without_an_active_grant_is_still_refused(client, setup, login_as):
    login_as("vrc_op14", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-NOGRANT-1")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "trying to edit directly without a grant", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
        ],
    })
    # The route's own Operator grant-check intercepts before correct_record()
    # is ever reached (no active grant exists) — 403, not 400. Still fully
    # refused either way; see test_manager_still_cannot_directly_correct_a_
    # voided_record_without_a_grant below for the 400 correct_record() itself
    # raises when an elevated caller reaches it directly with no grant.
    assert res.status_code == 403


def test_manager_still_cannot_directly_correct_a_voided_record_without_a_grant(client, setup, login_as):
    login_as("vrc_op15", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-MGRDIRECT-1")
    login_as("vrc_mgr10", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "manager trying to edit a void record directly", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
        ],
    })
    assert res.status_code == 400


def test_an_already_void_record_still_cannot_be_voided_again(client, setup, login_as):
    login_as("vrc_op16", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-DOUBLEVOID-1")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "trying again"})
    assert res.status_code == 400


# =====================================================================
# 15: full audit trail
# =====================================================================

def test_full_audit_trail_exists_for_a_voided_record_correction(client, app, setup, login_as):
    login_as("vrc_op17", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-D-AUDIT-1", cartons=5)
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "Needs a correction while void, for audit trail proof",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_mgr11", "password123", "manager")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    _relogin(client, "vrc_op17")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "Audit trail proof correction", "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0},
        ],
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        create_entry = AuditLog.query.filter_by(action="create", entity_type="correction_request", entity_id=str(req["id"])).first()
        approve_entry = AuditLog.query.filter_by(action="approve", entity_type="correction_request", entity_id=str(req["id"])).first()
        correct_entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch", entity_id=str(d["id"])).first()
        assert create_entry is not None
        assert approve_entry is not None
        assert correct_entry is not None
        after = json.loads(correct_entry.after_json)
        assert after["via_request_id"] == req["id"]
        assert after["correction_source"] == "approved_grant"
        assert after["void_status_preserved"] is True
        assert after["status_after"] == "void"


# =====================================================================
# Frontend: button matrix offers Request Correction / grant-based Edit
# on void records, never a direct Edit, never for Manager/Super Admin
# =====================================================================

@pytest.mark.parametrize("html", [DISPATCH_HTML, RETURNS_HTML, PRODUCTION_HTML], ids=["dispatch", "returns", "production"])
def test_void_status_branch_offers_request_correction_never_direct_edit_for_elevated(html):
    idx = html.index("} else if(data.status === 'void'){")
    end = html.index("} else {", idx)
    body = html[idx:end]
    # "isElevated" may still appear in an explanatory comment (documenting
    # why it's excluded) — what must never appear is an actual CONDITION
    # referencing it, which would grant Manager/Super Admin a direct Edit
    # on a void record.
    assert "isElevated ||" not in body
    assert "isElevated &&" not in body
    assert "if(isElevated" not in body
    assert "request-correct" in body
    assert 'data-action="correct"' in body


# =====================================================================
# Dependency: pywebpush is a real, active requirements.txt entry
# =====================================================================

def test_pywebpush_is_an_active_uncommented_requirement():
    lines = [ln.strip() for ln in REQUIREMENTS_TXT.splitlines()]
    assert "pywebpush==2.0.1" in lines  # exact, uncommented line — not "# pywebpush==..."


def test_dockerfile_installs_from_requirements_txt():
    dockerfile = (pathlib.Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install" in dockerfile and "requirements.txt" in dockerfile


def test_no_vapid_private_key_literal_in_requirements_or_dockerfile():
    dockerfile = (pathlib.Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    for content in (REQUIREMENTS_TXT, dockerfile):
        assert "VAPID_PRIVATE_KEY=" not in content
        assert "VAPID_PRIVATE_KEY = " not in content


# =====================================================================
# App starts / push safety with VAPID missing (still no manual install
# needed for the app itself to function — push_service degrades safely)
# =====================================================================

def test_app_starts_and_functions_safely_with_vapid_config_missing(client, setup, login_as):
    from webapp.services import push_service
    assert push_service.is_configured() is False
    login_as("vrc_startup_op", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-STARTUP-1")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "app must still function without vapid configured",
        "payload": {"lines": []},
    })
    assert res.status_code == 201


def test_push_sender_gracefully_no_ops_without_configuration(client, app):
    from webapp.services import push_service
    with app.app_context():
        # Never raises, never requires an external network call.
        push_service.notify_users([1], "Title", "Body", "/requests.html", badge_count=3)


# =====================================================================
# Background push handler — service worker
# =====================================================================

def test_service_worker_has_a_real_push_handler_calling_show_notification():
    idx = SW_JS.index("self.addEventListener('push'")
    end = SW_JS.index("});", idx)
    body = SW_JS[idx:end]
    assert "showNotification" in body


def test_service_worker_notificationclick_routes_to_requests():
    idx = SW_JS.index("self.addEventListener('notificationclick'")
    end = SW_JS.index("});", idx)
    body = SW_JS[idx:end]
    assert "/requests.html" in body


def test_service_worker_feature_detects_background_badge_api():
    idx = SW_JS.index("function updateBackgroundBadge(")
    end = SW_JS.index("\n}", idx)
    body = SW_JS[idx:end]
    assert "'setAppBadge' in self.navigator" in body
    assert "try {" in body
    assert "catch (e)" in body


def test_service_worker_push_handler_applies_badge_count_from_payload():
    idx = SW_JS.index("self.addEventListener('push'")
    end = SW_JS.index("});", SW_JS.index("updateBackgroundBadge(payload.badgeCount)", idx))
    body = SW_JS[idx:end]
    assert "updateBackgroundBadge(payload.badgeCount)" in body


def test_notify_new_correction_request_includes_authoritative_badge_count(app, setup, client, login_as):
    login_as("vrc_badge_op", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-BADGE-1")
    with app.app_context():
        from webapp.services import push_service, correction_request_service
        calls = []
        original = push_service.notify_users

        def _spy(user_ids, title, body, url, badge_count=None):
            calls.append(badge_count)
            return original(user_ids, title, body, url, badge_count=badge_count)

        push_service.notify_users = _spy
        try:
            client.post("/api/correction-requests", json={
                "record_type": "dispatch", "record_id": d["id"], "action": "correct",
                "reason": "checking badge count is threaded through to push",
                "payload": {"lines": []},
            })
        finally:
            push_service.notify_users = original
        assert calls == [correction_request_service.pending_count()]


def test_operator_decision_notifications_never_carry_a_badge_count(app, setup, client, login_as):
    # Operators have no Requests badge in this app — notify_request_decided()
    # must never pass a badge_count (it stays the default None).
    login_as("vrc_nobadge_op", "password123", "operator")
    d = _void_dispatch(client, setup, number="VRC-NOBADGE-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "checking no badge count on decision notifications",
        "payload": {"lines": []},
    }).get_json()
    login_as("vrc_nobadge_mgr", "password123", "manager")
    with app.app_context():
        from webapp.services import push_service
        calls = []
        original = push_service.notify_users

        def _spy(user_ids, title, body, url, badge_count=None):
            calls.append(badge_count)
            return original(user_ids, title, body, url, badge_count=badge_count)

        push_service.notify_users = _spy
        try:
            client.post(f"/api/correction-requests/{req['id']}/approve", json={})
        finally:
            push_service.notify_users = original
        assert calls == [None]
