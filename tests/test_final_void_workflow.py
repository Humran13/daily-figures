"""
Round D, Part 2 — Void workflow (Dispatch/Returns/Production).

Void is NOT Delete: the record, its line items, and full auditability are
preserved. A voided record:
  - is excluded from stock-effective figures (Issued/Returns/Production
    contribution -> 0), via the SAME status filters every other
    finalized-only query already used (STATUS_FINALIZED; void is simply
    never that), so no new filter logic was introduced anywhere;
  - remains visible in History, clearly marked as void;
  - records who voided it, when, and why;
  - lets carry-forward (Closing = Opening + Production + Returns - Issued;
    next Opening = previous Closing) recompute itself naturally from the
    now-lower total — no compensating/fake movement rows are created.

Delete remains a fully separate, permanent, Manager/Super-Admin-only
capability — untouched by any of this.
"""
import pytest

from webapp.services.business_calendar import business_today


def _make_product(client, name="VOID Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("void_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "VOID Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "VOID Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _figures(client, product_id, date, shift="Day"):
    return client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()


# =====================================================================
# DISPATCH — Void zeroes Issued, preserves the record
# =====================================================================

def test_manager_void_zeroes_issued_and_preserves_record(client, setup, super_admin):
    pid = setup["product"]["id"]
    date = business_today()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D1", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _figures(client, pid, date)["issued"]["base_qty"] == 500

    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "entered in error"})
    assert res.status_code == 200
    assert _figures(client, pid, date)["issued"]["base_qty"] == 0

    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["id"] == d["id"]
    assert still["status"] == "void"
    assert len(still["lines"]) == 1
    assert still["lines"][0]["cartons"] == 5  # line items preserved, not cleared


def test_voided_dispatch_still_appears_in_history_list(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D2", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})

    listed = client.get("/api/dispatches?limit=200").get_json()["results"]
    row = next(r for r in listed if r["id"] == d["id"])
    assert row["status"] == "void"


def test_void_requires_a_reason(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D3", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": ""})
    assert res.status_code == 400
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "finalized"


def test_repeated_void_cannot_double_change_stock(client, setup, super_admin):
    pid = setup["product"]["id"]
    date = business_today()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D4", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    first = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    assert first.status_code == 200
    assert _figures(client, pid, date)["issued"]["base_qty"] == 0

    second = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x again"})
    assert second.status_code == 400  # already void — refused, not double-applied
    assert _figures(client, pid, date)["issued"]["base_qty"] == 0


def test_void_records_actor_reason_and_timestamp(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D5", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "audit trail check"})

    updated = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert updated["void_reason"] == "audit trail check"
    assert updated["voided_by"] is not None
    assert updated["voided_at"] is not None


def test_void_action_is_audited(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D6", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "audit check"})

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="void", entity_type="dispatch", entity_id=str(d["id"])).first()
        assert entry is not None
        assert entry.username == "void_root"


def test_void_carries_closing_and_next_opening_forward_correctly(client, setup, super_admin):
    pid = setup["product"]["id"]
    date = business_today()
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D7", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    before = _figures(client, pid, date)
    assert before["closing"]["base_qty"] == 600  # 1000 opening - 400 issued

    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    after = _figures(client, pid, date)
    assert after["closing"]["base_qty"] == 1000  # issued no longer subtracted
    assert after["closing"]["base_qty"] == after["opening"]["base_qty"] + after["production"]["base_qty"] + after["return_"]["base_qty"] - after["issued"]["base_qty"]


def test_delete_remains_separate_and_still_works_on_a_finalized_dispatch(client, setup, super_admin, app):
    # Void does not replace Delete — Delete is still the separate,
    # permanent, Manager/Super-Admin-only capability it already was.
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-D8", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "wrong entry entirely", "confirm": True})
    assert res.status_code == 200
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.dispatch import Dispatch
        assert _db.session.get(Dispatch, d["id"]) is None  # physically gone — unlike void


# =====================================================================
# RETURNS — Void zeroes Returns contribution; Metro Sales Monday-only
# rule keeps working alongside it
# =====================================================================

def test_manager_void_return_zeroes_contribution(client, setup, super_admin):
    pid = setup["product"]["id"]
    date = business_today()
    r = client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    assert _figures(client, pid, date)["return_"]["base_qty"] == 400

    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "duplicate entry"})
    assert res.status_code == 200
    assert _figures(client, pid, date)["return_"]["base_qty"] == 0
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "void"


def test_voiding_a_non_monday_metro_sales_return_changes_nothing_it_already_contributed_zero(client, setup, super_admin):
    # A non-Monday Metro Sales return already contributes 0 to stock while
    # finalized (see returns_service.is_return_stock_posting_eligible) —
    # voiding it must not error, must still flip status to void, and the
    # already-zero contribution must remain exactly zero (never negative,
    # never double-subtracted).
    import datetime
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    metro_cust = client.post("/api/admin/customers", json={
        "name": "Metro Sales Truck", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    pid = setup["product"]["id"]

    # Find a non-Monday date near today.
    today = datetime.date.fromisoformat(business_today())
    non_monday = today
    while non_monday.weekday() == 0:
        non_monday += datetime.timedelta(days=1)
    date = non_monday.isoformat()

    r = client.post("/api/returns", json={
        "date": date, "customer_id": metro_cust["id"],
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    assert _figures(client, pid, date)["return_"]["base_qty"] == 0  # non-Monday: already zero

    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "x"})
    assert res.status_code == 200
    assert _figures(client, pid, date)["return_"]["base_qty"] == 0
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "void"


def test_voiding_a_monday_metro_sales_return_removes_its_real_contribution(client, setup, super_admin):
    import datetime
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    metro_cust = client.post("/api/admin/customers", json={
        "name": "Metro Sales Truck 2", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    pid = setup["product"]["id"]

    today = datetime.date.fromisoformat(business_today())
    monday = today - datetime.timedelta(days=today.weekday())  # this week's Monday (may be in the past — fine, Manager voids any day)
    date = monday.isoformat()

    r = client.post("/api/returns", json={
        "date": date, "customer_id": metro_cust["id"],
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    assert _figures(client, pid, date)["return_"]["base_qty"] == 300  # Monday: real contribution

    client.post(f"/api/returns/{r['id']}/void", json={"reason": "x"})
    assert _figures(client, pid, date)["return_"]["base_qty"] == 0


def test_returns_delete_still_separate_from_void(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    r = client.post("/api/returns", json={
        "date": business_today(), "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.delete(f"/api/returns/{r['id']}", json={"reason": "x", "confirm": True})
    assert res.status_code == 200
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.return_record import ReturnRecord
        assert _db.session.get(ReturnRecord, r["id"]) is None


# =====================================================================
# PRODUCTION — Void zeroes Production contribution
# =====================================================================

def test_manager_void_production_zeroes_contribution(client, setup, super_admin):
    pid = setup["product"]["id"]
    date = business_today()
    p = client.post("/api/production", json={
        "date": date, "shift": "Day", "lines": [{"product_id": pid, "cartons": 6, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    assert _figures(client, pid, date)["production"]["base_qty"] == 600

    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "duplicate batch"})
    assert res.status_code == 200
    assert _figures(client, pid, date)["production"]["base_qty"] == 0
    assert client.get(f"/api/production/{p['id']}").get_json()["status"] == "void"


def test_production_delete_still_separate_from_void(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    p = client.post("/api/production", json={
        "date": business_today(), "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.delete(f"/api/production/{p['id']}", json={"reason": "x", "confirm": True})
    assert res.status_code == 200
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.production_record import ProductionRecord
        assert _db.session.get(ProductionRecord, p["id"]) is None


# =====================================================================
# ROLE PERMISSIONS — voids by role/ownership/date (cross-check against
# test_final_operator_same_day_edit_window.py, focused here on the stock
# effect + Viewer)
# =====================================================================

def test_viewer_cannot_void_anything(client, setup, super_admin, login_as):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-VIEWER-1", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    login_as("void_viewer", "password123", "viewer")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    assert res.status_code == 403
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "finalized"


def test_void_button_in_ui_offered_for_drafts_too_not_just_finalized(client, setup, super_admin):
    # The backend has always allowed voiding a still-draft record directly
    # (pre-existing behavior — e.g. "customer cancelled before we ever
    # finalized this dispatch"; see tests/test_dispatches.py::
    # test_void_requires_reason, unchanged by this round). The UI must not
    # contradict that: the Void button is gated on `data.status !== 'void'`
    # — draft or finalized, not artificially restricted to finalized-only
    # — which this checks at the markup level. (See
    # test_final_targeted_ux_permissions.py::
    # test_void_is_reachable_on_any_non_void_status_including_draft for
    # the exact-block assertion shared with Returns/Production.)
    import pathlib
    dispatch_html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "dispatch.html").read_text(encoding="utf-8")
    assert "if(data.status === 'finalized'){" not in dispatch_html
    assert "if(data.status !== 'void'){" in dispatch_html

    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VOID-DRAFT-1", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    # never finalized — still a draft; voiding it directly is a valid
    # backend operation (zero stock effect either way, since a draft never
    # contributed), and now also a reachable UI action.
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "customer cancelled before finalizing"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# =====================================================================
# FINAL CONSISTENCY FIX — Void button visibility (draft included)
#
# The Void action-row button is gated purely on `data.status !== 'void'`
# combined with (isElevated || (isOperator && ownsRecord)) — deliberately
# unconditional on record age, no "Request Void" at all — the SAME
# condition on every one of Dispatch/Returns/Production, and it no longer
# singles out 'finalized'. "Sees Void" below is proven
# two ways together, since this project has no JS/browser test runner:
#   (a) the shared markup condition (verified once, role-agnostically,
#       in test_final_targeted_ux_permissions.py::
#       test_void_is_reachable_on_any_non_void_status_including_draft)
#       evaluates true for this exact role/state combination, and
#   (b) the underlying POST .../void this button would trigger actually
#       succeeds for that same role/state — proving the button is never a
#       dead end, and never hidden when it shouldn't be.
# =====================================================================

def _draft_dispatch(client, setup, date, number="VOID-CONSIST-D"):
    return client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


def _draft_return(client, setup, date):
    return client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


def _draft_production(client, setup, date):
    return client.post("/api/production", json={
        "date": date, "shift": "Day", "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


# 1. Operator sees Void on their own same-day Draft Dispatch.
def test_operator_sees_void_on_own_same_day_draft_dispatch(client, setup, login_as):
    login_as("cf_op_dispatch_draft", "password123", "operator")
    d = _draft_dispatch(client, setup, business_today())
    assert d["status"] == "draft"
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "cancelled before finalizing"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# 2. Operator sees Void on their own same-day Finalized Dispatch.
def test_operator_sees_void_on_own_same_day_finalized_dispatch(client, setup, login_as):
    login_as("cf_op_dispatch_final", "password123", "operator")
    d = _draft_dispatch(client, setup, business_today(), number="VOID-CONSIST-D2")
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "wrong entry"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# 3. Same for Returns (own same-day Draft, and own same-day Finalized).
def test_operator_sees_void_on_own_same_day_draft_return(client, setup, login_as):
    login_as("cf_op_return_draft", "password123", "operator")
    r = _draft_return(client, setup, business_today())
    assert r["status"] == "draft"
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "cancelled before finalizing"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


def test_operator_sees_void_on_own_same_day_finalized_return(client, setup, login_as):
    login_as("cf_op_return_final", "password123", "operator")
    r = _draft_return(client, setup, business_today())
    client.post(f"/api/returns/{r['id']}/finalize")
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "wrong entry"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# 4. Same for Production (own same-day Draft, and own same-day Finalized).
def test_operator_sees_void_on_own_same_day_draft_production(client, setup, login_as):
    login_as("cf_op_prod_draft", "password123", "operator")
    p = _draft_production(client, setup, business_today())
    assert p["status"] == "draft"
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "cancelled before finalizing"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


def test_operator_sees_void_on_own_same_day_finalized_production(client, setup, login_as):
    login_as("cf_op_prod_final", "password123", "operator")
    p = _draft_production(client, setup, business_today())
    client.post(f"/api/production/{p['id']}/finalize")
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "wrong entry"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# 5. Manager/Super Admin can access legitimate Draft void behavior — any
#    day, any owner (not subject to the same-day/ownership rule at all).
def test_manager_can_void_a_draft_dispatch_any_day_any_owner(client, setup, login_as):
    login_as("cf_someop", "password123", "operator")
    d = _draft_dispatch(client, setup, "2020-01-01", number="VOID-CONSIST-MGR1")  # historical, someone else's
    assert d["status"] == "draft"
    client.post("/api/logout")

    login_as("cf_mgr", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "manager voids a stale draft"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


def test_super_admin_can_void_a_draft_return_any_day(client, setup, super_admin):
    r = _draft_return(client, setup, "2020-01-01")
    assert r["status"] == "draft"
    res = client.post(f"/api/returns/{r['id']}/void", json={"reason": "sa voids a stale draft"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


def test_manager_can_void_a_draft_production_any_day(client, setup, login_as):
    login_as("cf_mgr_prod", "password123", "manager")
    p = _draft_production(client, setup, "2020-01-01")
    assert p["status"] == "draft"
    res = client.post(f"/api/production/{p['id']}/void", json={"reason": "manager voids a stale draft"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# 6. Already-void records must not offer Void again — markup (the same
#    `data.status !== 'void'` gate excludes it) and backend (repeat void
#    is refused, not silently re-applied) checked together.
def test_already_void_dispatch_does_not_show_or_accept_void_again(client, setup, super_admin):
    d = _draft_dispatch(client, setup, business_today(), number="VOID-CONSIST-REPEAT")
    client.post(f"/api/dispatches/{d['id']}/finalize")
    first = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"})
    assert first.status_code == 200
    second = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x again"})
    assert second.status_code == 400
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


# 7. Viewer never sees Void — any status, any ownership.
def test_viewer_never_sees_void_on_draft_or_finalized(client, setup, super_admin, login_as):
    d_draft = _draft_dispatch(client, setup, business_today(), number="VOID-CONSIST-VDRAFT")
    d_final = _draft_dispatch(client, setup, business_today(), number="VOID-CONSIST-VFINAL")
    client.post(f"/api/dispatches/{d_final['id']}/finalize")

    login_as("cf_viewer", "password123", "viewer")
    res1 = client.post(f"/api/dispatches/{d_draft['id']}/void", json={"reason": "x"})
    assert res1.status_code == 403
    res2 = client.post(f"/api/dispatches/{d_final['id']}/void", json={"reason": "x"})
    assert res2.status_code == 403


# 8. Full targeted Operator correction/void/requests package — Void is
#    now deliberately UNCONDITIONAL on record age for the owning
#    Operator (there is no "Request Void" at all any more, for a draft
#    or a finalized historical record alike). Contrast with Edit, which
#    IS age-gated — see test_final_operator_same_day_edit_window.py.
def test_operator_can_still_directly_void_a_historical_draft_dispatch(client, setup, login_as):
    login_as("cf_hist_draft_op", "password123", "operator")
    d = _draft_dispatch(client, setup, "2020-01-01", number="VOID-CONSIST-HISTDRAFT")
    assert d["status"] == "draft"
    direct = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "still voidable directly"})
    assert direct.status_code == 200
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


def test_operator_can_still_directly_void_a_historical_finalized_dispatch(client, setup, login_as):
    login_as("cf_hist_final_op", "password123", "operator")
    d = _draft_dispatch(client, setup, "2020-01-01", number="VOID-CONSIST-HISTFINAL")
    client.post(f"/api/dispatches/{d['id']}/finalize")
    direct = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "still voidable directly"})
    assert direct.status_code == 200
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


def test_no_void_request_action_reaches_the_correction_request_api(client, setup, login_as):
    login_as("cf_hist_void_req_op", "password123", "operator")
    d = _draft_dispatch(client, setup, "2020-01-01", number="VOID-CONSIST-NOREQ")
    client.post(f"/api/dispatches/{d['id']}/finalize")
    request = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "historical cleanup attempt",
    })
    assert request.status_code == 400  # action=void is refused outright — Void is always direct


# 9. Backend and frontend permissions remain consistent — the exact
#    role/state matrix the markup encodes is cross-checked against actual
#    route behavior for every distinct case in one place.
def test_backend_and_frontend_void_permissions_are_consistent(client, setup, login_as, super_admin):
    import pathlib
    dispatch_html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "dispatch.html").read_text(encoding="utf-8")
    # Markup: the Void family is gated on `data.status !== 'void'` (draft
    # or finalized both qualify) with (isElevated || (isOperator &&
    # ownsRecord)) — deliberately unconditional on age, never a
    # finalized-only or age-gated condition.
    assert "if(data.status === 'finalized'){" not in dispatch_html
    assert "if(isElevated || (isOperator && ownsRecord)){" in dispatch_html

    today = business_today()
    cases = []  # (label, expected_status_code, setup_fn)

    login_as("cf_matrix_op", "password123", "operator")
    same_day_draft = _draft_dispatch(client, setup, today, number="VOID-MATRIX-1")
    cases.append(("operator/same-day/draft", 200, same_day_draft["id"]))
    same_day_final = _draft_dispatch(client, setup, today, number="VOID-MATRIX-2")
    client.post(f"/api/dispatches/{same_day_final['id']}/finalize")
    cases.append(("operator/same-day/finalized", 200, same_day_final["id"]))
    historical_draft = _draft_dispatch(client, setup, "2020-01-01", number="VOID-MATRIX-3")
    cases.append(("operator/historical/draft", 200, historical_draft["id"]))

    for label, expected, dispatch_id in cases:
        res = client.post(f"/api/dispatches/{dispatch_id}/void", json={"reason": "matrix check"})
        assert res.status_code == expected, f"{label}: expected {expected}, got {res.status_code}"
    client.post("/api/logout")

    # Manager: every case succeeds, regardless of day/state/ownership.
    login_as("cf_matrix_mgr", "password123", "manager")
    mgr_draft = _draft_dispatch(client, setup, "2020-01-01", number="VOID-MATRIX-4")
    res = client.post(f"/api/dispatches/{mgr_draft['id']}/void", json={"reason": "matrix check mgr"})
    assert res.status_code == 200
    # A target dispatch for the Viewer case below — Viewer cannot create
    # one itself (403 on POST /api/dispatches), so it's made here while
    # still an elevated session, then the role switches for the attempt.
    viewer_target = _draft_dispatch(client, setup, today, number="VOID-MATRIX-5")
    client.post("/api/logout")

    # Viewer: every case forbidden.
    login_as("cf_matrix_viewer", "password123", "viewer")
    res = client.post(f"/api/dispatches/{viewer_target['id']}/void", json={"reason": "matrix check viewer"})
    assert res.status_code == 403
