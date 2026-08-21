"""
Full targeted Operator correction/void/requests/notification package —
the Operator direct Edit window (formerly "same business day" — see git
history for that superseded rule) is now an EXACT 24 hours measured from
the record's own `created_at`, never the business Date, never the
browser clock. See webapp/services/record_correction_service.py's
operator_can_directly_edit().

Void is a SEPARATE, deliberately UNCONDITIONAL-on-age direct action for
an Operator's own record — operator_can_directly_void() — so this file's
Void coverage only proves "still works after the 24h Edit window has
closed", never "blocked after 24h" (there is no such block for Void at
all; see test_final_void_workflow.py for the full Void suite).

Tests use directly-injected/backdated `created_at` timestamps (never a
real wall-clock sleep) for deterministic, non-flaky boundary behavior —
freezegun is not installed in this environment, so `record.created_at`
is set directly via the DB session inside `app.app_context()`, which is
exactly equivalent for this purpose since operator_can_directly_edit()
only ever reads that one column.
"""
import datetime

import pytest

from webapp.services.business_calendar import utcnow
from webapp.services.record_correction_service import OPERATOR_EDIT_WINDOW


def _make_product(client, name="SDW Product", cartons_to_packs=10, packs_to_pieces=10):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces,
    })
    return product


def _make_customer(client, category_id, name="SDW Recipient"):
    return client.post("/api/admin/customers", json={
        "name": name, "sales_category_id": category_id, "confirm_not_duplicate": True,
    }).get_json()


@pytest.fixture
def super_admin(login_as):
    return login_as("sdw_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "SDW Category"}).get_json()
    customer = _make_customer(client, category["id"])
    return {"product": product, "category": category, "customer": customer}


def _make_dispatch(client, setup, dispatch_number="SDW-D1", cartons=1):
    d = client.post("/api/dispatches", json={
        "dispatch_number": dispatch_number, "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


def _make_return(client, setup, cartons=1):
    r = client.post("/api/returns", json={
        "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    return r


def _make_production(client, setup, shift="Day", cartons=1):
    p = client.post("/api/production", json={
        "date": "2020-01-01", "shift": shift,
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    return p


def _backdate(app, model, record_id, age):
    """Sets record.created_at to exactly `age` (a timedelta) before the
    real current instant — the one deterministic control point every
    test in this file uses instead of a real wall-clock sleep."""
    with app.app_context():
        from webapp.extensions import db
        row = db.session.get(model, record_id)
        row.created_at = utcnow() - age
        db.session.commit()


def _dispatch_model():
    from webapp.models.dispatch import Dispatch
    return Dispatch


def _return_model():
    from webapp.models.return_record import ReturnRecord
    return ReturnRecord


def _production_model():
    from webapp.models.production_record import ProductionRecord
    return ProductionRecord


# =====================================================================
# Exact 24-hour boundary — Dispatch
# =====================================================================

def test_operator_can_edit_at_23h59m59s_after_creation(client, setup, login_as, app):
    login_as("sdw_op1", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(hours=23, minutes=59, seconds=59))
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "still within window", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_edit_at_exactly_24h_after_creation(client, setup, login_as, app):
    login_as("sdw_op2", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], OPERATOR_EDIT_WINDOW)
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "right at the cutoff", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_operator_cannot_edit_well_past_24h(client, setup, login_as, app):
    login_as("sdw_op3", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(hours=25))
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403
    assert "request correction" in res.get_json()["error"].lower()


def test_request_correction_appears_only_after_24h(client, setup, login_as, app):
    login_as("sdw_op4", "password123", "operator")
    d = _make_dispatch(client, setup)
    # Still within the window: a request is refused (409) — direct Edit
    # is the correct action here, never a request.
    still_fresh = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "trying too early anyway",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}]},
    })
    assert still_fresh.status_code == 409

    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(hours=25))
    now_allowed = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct", "reason": "past the 24h window now",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}]},
    })
    assert now_allowed.status_code == 201


def test_server_rejects_forged_direct_edit_after_expiry_regardless_of_client_claims(client, setup, login_as, app):
    login_as("sdw_forge_op", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(hours=25))
    # No matter what extra fields a forged request sends, the route only
    # ever reads the record's own stored created_at and the server's own
    # current instant — never anything client-supplied about "now".
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "forged claim", "notes": None, "client_now": utcnow().isoformat(), "force_within_window": True,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_another_operator_cannot_edit_even_within_the_24h_window(client, setup, login_as):
    login_as("sdw_owner", "password123", "operator")
    d = _make_dispatch(client, setup)
    client.post("/api/logout")

    login_as("sdw_notowner", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "not mine", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


# =====================================================================
# Same 24-hour boundary — Returns
# =====================================================================

def test_operator_can_edit_return_within_24h(client, setup, login_as):
    login_as("sdw_rop1", "password123", "operator")
    r = _make_return(client, setup)
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "fresh edit", "notes": None,
        "lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_edit_return_past_24h(client, setup, login_as, app):
    login_as("sdw_rop2", "password123", "operator")
    r = _make_return(client, setup)
    _backdate(app, _return_model(), r["id"], datetime.timedelta(hours=24, minutes=1))
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


# =====================================================================
# Same 24-hour boundary — Production
# =====================================================================

def test_operator_can_edit_production_within_24h(client, setup, login_as):
    login_as("sdw_pop1", "password123", "operator")
    p = _make_production(client, setup)
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fresh edit", "notes": None,
        "lines": [{"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_edit_production_past_24h(client, setup, login_as, app):
    login_as("sdw_pop2", "password123", "operator")
    p = _make_production(client, setup)
    _backdate(app, _production_model(), p["id"], datetime.timedelta(hours=24, minutes=1))
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


# =====================================================================
# Void — deliberately unconditional on age (contrast with Edit above)
# =====================================================================

def test_operator_can_still_void_own_dispatch_long_after_24h(client, setup, login_as, app):
    login_as("sdw_void_op", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(days=30))
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "still voidable after 30 days"})
    assert res.status_code == 200
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


def test_another_operator_still_cannot_void_regardless_of_age(client, setup, login_as, app):
    login_as("sdw_void_owner", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(days=30))
    client.post("/api/logout")

    login_as("sdw_void_notowner", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "not mine"})
    assert res.status_code == 403


# =====================================================================
# Manager/Super Admin are never constrained by the Operator's 24-hour
# rule, at all.
# =====================================================================

def test_manager_can_correct_any_record_any_age_regardless_of_owner(client, setup, login_as, app):
    login_as("sdw_someop", "password123", "operator")
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(days=100))
    client.post("/api/logout")

    login_as("sdw_mgr", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "manager correction", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_super_admin_can_correct_and_void_any_age(client, setup, super_admin, app):
    d = _make_dispatch(client, setup)
    _backdate(app, _dispatch_model(), d["id"], datetime.timedelta(days=100))
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "sa correction", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200

    d2 = _make_dispatch(client, setup, dispatch_number="SDW-D2")
    _backdate(app, _dispatch_model(), d2["id"], datetime.timedelta(days=100))
    res2 = client.post(f"/api/dispatches/{d2['id']}/void", json={"reason": "sa void"})
    assert res2.status_code == 200


# =====================================================================
# Viewer never reaches either action, regardless of age/ownership
# =====================================================================

def test_viewer_cannot_correct_or_void_anything(client, setup, super_admin, login_as):
    d = _make_dispatch(client, setup)
    client.post("/api/logout")

    login_as("sdw_viewer", "password123", "viewer")
    res1 = client.post(f"/api/dispatches/{d['id']}/correct", json={"reason": "x", "lines": []})
    assert res1.status_code == 403
    res2 = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    assert res2.status_code == 403


# =====================================================================
# Display — all user-facing times use Kampala (Africa/Kampala, UTC+3),
# never raw UTC.
# =====================================================================

def test_session_exposes_server_now_for_ux_only(client, super_admin):
    res = client.get("/api/session")
    assert res.status_code == 200
    body = res.get_json()
    assert "server_now" in body
    datetime.datetime.fromisoformat(body["server_now"])


def test_dispatch_created_at_label_is_kampala_not_utc(client, setup, login_as):
    login_as("sdw_kampala_op", "password123", "operator")
    d = _make_dispatch(client, setup)
    fetched = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert fetched["created_at_label"] is not None
    # The label is always 3 hours ahead of the raw UTC created_at value —
    # a direct proof the display path is Kampala, not UTC passthrough.
    from webapp.services.business_calendar import format_kampala_datetime
    raw = datetime.datetime.fromisoformat(fetched["created_at"])
    assert fetched["created_at_label"] == format_kampala_datetime(raw)
