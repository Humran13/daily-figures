"""
Round D, Part 1 — Controlled Operator editing window.

An Operator may directly correct/void a Dispatch/Returns/Production
record only while BOTH:
  - they created it (ownership), and
  - its own business `date` is still today, in Africa/Kampala (a fixed
    UTC+3 offset — see webapp/services/business_calendar.py's own
    docstring for why zoneinfo/tzdata isn't used here).

Once the record's date is no longer today, direct /correct and /void are
refused (403) for an Operator — they must use the Request Correction /
Request Void workflow instead (see test_final_correction_requests.py).
Manager/Super Admin are never subject to this rule; Viewer can never
reach either action regardless of date/ownership.

None of this touches stock formulas, packaging rules, or Ledger Cutover.
"""
import datetime

import pytest

from webapp.services.business_calendar import business_today, is_same_business_day


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


def _yesterday():
    return (datetime.date.fromisoformat(business_today()) - datetime.timedelta(days=1)).isoformat()


def _make_dispatch(client, setup, date, dispatch_number="SDW-D1", cartons=1):
    return client.post("/api/dispatches", json={
        "dispatch_number": dispatch_number, "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()


def _make_return(client, setup, date, cartons=1):
    return client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()


def _make_production(client, setup, date, shift="Day", cartons=1):
    return client.post("/api/production", json={
        "date": date, "shift": shift,
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()


# =====================================================================
# business_calendar itself
# =====================================================================

def test_business_today_is_a_date_string():
    today = business_today()
    datetime.date.fromisoformat(today)  # raises if malformed


def test_is_same_business_day_matches_business_today():
    assert is_same_business_day(business_today()) is True
    assert is_same_business_day(_yesterday()) is False


# =====================================================================
# DISPATCH — direct correct / void, same-day vs historical
# =====================================================================

def test_operator_can_correct_own_dispatch_dated_today(client, setup, login_as):
    login_as("sdw_op1", "password123", "operator")
    d = _make_dispatch(client, setup, business_today())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "same-day fix", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_directly_correct_own_dispatch_dated_yesterday(client, setup, login_as):
    login_as("sdw_op2", "password123", "operator")
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403
    assert "request" in res.get_json()["error"].lower()


def test_operator_cannot_correct_another_operators_dispatch_even_same_day(client, setup, login_as):
    login_as("sdw_owner", "password123", "operator")
    d = _make_dispatch(client, setup, business_today())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/logout")

    login_as("sdw_notowner", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "not mine", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_operator_can_void_own_dispatch_dated_today(client, setup, login_as):
    login_as("sdw_op3", "password123", "operator")
    d = _make_dispatch(client, setup, business_today())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "same-day void"})
    assert res.status_code == 200
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


def test_operator_cannot_directly_void_own_dispatch_dated_yesterday(client, setup, login_as):
    login_as("sdw_op4", "password123", "operator")
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "too late"})
    assert res.status_code == 403
    assert "request" in res.get_json()["error"].lower()


def test_operator_cannot_void_another_operators_dispatch_even_same_day(client, setup, login_as):
    login_as("sdw_owner2", "password123", "operator")
    d = _make_dispatch(client, setup, business_today())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/logout")

    login_as("sdw_notowner2", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "not mine"})
    assert res.status_code == 403


# =====================================================================
# RETURNS — same shape
# =====================================================================

def test_operator_can_correct_own_return_dated_today(client, setup, login_as):
    login_as("sdw_rop1", "password123", "operator")
    r = _make_return(client, setup, business_today())
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "same-day fix", "notes": None,
        "lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_directly_correct_own_return_dated_yesterday(client, setup, login_as):
    login_as("sdw_rop2", "password123", "operator")
    r = _make_return(client, setup, _yesterday())
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": r["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_operator_can_void_own_return_dated_today(client, setup, login_as):
    login_as("sdw_rop3", "password123", "operator")
    r = _make_return(client, setup, business_today())
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "same-day void"})
    assert res.status_code == 200
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "void"


def test_operator_cannot_directly_void_own_return_dated_yesterday(client, setup, login_as):
    login_as("sdw_rop4", "password123", "operator")
    r = _make_return(client, setup, _yesterday())
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "too late"})
    assert res.status_code == 403


# =====================================================================
# PRODUCTION — same shape
# =====================================================================

def test_operator_can_correct_own_production_dated_today(client, setup, login_as):
    login_as("sdw_pop1", "password123", "operator")
    p = _make_production(client, setup, business_today())
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "same-day fix", "notes": None,
        "lines": [{"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_directly_correct_own_production_dated_yesterday(client, setup, login_as):
    login_as("sdw_pop2", "password123", "operator")
    p = _make_production(client, setup, _yesterday())
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "too late", "notes": None,
        "lines": [{"id": p["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_operator_can_void_own_production_dated_today(client, setup, login_as):
    login_as("sdw_pop3", "password123", "operator")
    p = _make_production(client, setup, business_today())
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "same-day void"})
    assert res.status_code == 200
    assert client.get(f"/api/production/{p['id']}").get_json()["status"] == "void"


def test_operator_cannot_directly_void_own_production_dated_yesterday(client, setup, login_as):
    login_as("sdw_pop4", "password123", "operator")
    p = _make_production(client, setup, _yesterday())
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "too late"})
    assert res.status_code == 403


# =====================================================================
# Manager/Super Admin bypass the same-day rule entirely (any day, any
# owner — this is the correction authority the rule specifically
# preserves for them).
# =====================================================================

def test_manager_can_correct_any_historical_record_regardless_of_owner(client, setup, login_as):
    login_as("sdw_someop", "password123", "operator")
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/logout")

    login_as("sdw_mgr", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "manager correction", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_manager_can_void_any_historical_record_regardless_of_owner(client, setup, login_as):
    login_as("sdw_someop2", "password123", "operator")
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/logout")

    login_as("sdw_mgr2", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "manager void"})
    assert res.status_code == 200


def test_super_admin_can_correct_and_void_any_day(client, setup, super_admin):
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "sa correction", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200

    d2 = _make_dispatch(client, setup, _yesterday(), dispatch_number="SDW-D2")
    client.post(f"/api/dispatches/{d2['id']}/finalize")
    res2 = client.post(f"/api/dispatches/{d2['id']}/void", json={"reason": "sa void"})
    assert res2.status_code == 200


# =====================================================================
# Viewer never reaches either action, regardless of date/ownership
# =====================================================================

def test_viewer_cannot_correct_or_void_anything(client, setup, super_admin, login_as):
    d = _make_dispatch(client, setup, business_today())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post("/api/logout")

    login_as("sdw_viewer", "password123", "viewer")
    res1 = client.post(f"/api/dispatches/{d['id']}/correct", json={"reason": "x", "lines": []})
    assert res1.status_code == 403
    res2 = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    assert res2.status_code == 403


# =====================================================================
# Server-side enforcement — a forged/omitted date on the frontend cannot
# bypass the rule; the SERVER re-derives business_today() itself and
# re-reads the record's own stored date, never trusting anything the
# client sends about "today".
# =====================================================================

def test_server_ignores_a_forged_client_supplied_today_claim(client, setup, login_as):
    login_as("sdw_forge_op", "password123", "operator")
    d = _make_dispatch(client, setup, _yesterday())
    client.post(f"/api/dispatches/{d['id']}/finalize")
    # Even if a caller sends extra fields claiming "today" or a client-side
    # override, the route never reads any such field — only the record's
    # own stored `date` column and the server's own business_today().
    res = client.post(f"/api/dispatches/{d['id']}/void", json={
        "reason": "trying to forge same-day", "client_today": business_today(), "force_same_day": True,
    })
    assert res.status_code == 403


def test_session_exposes_business_today_for_ux_only(client, super_admin):
    res = client.get("/api/session")
    assert res.status_code == 200
    body = res.get_json()
    assert body["business_today"] == business_today()
    datetime.date.fromisoformat(body["business_today"])
