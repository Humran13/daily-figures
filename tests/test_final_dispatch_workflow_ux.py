"""
Dispatch workflow + History + Dashboard UX improvements (final consolidated
round): Operator draft editing (reopen -> edit -> re-save -> finalize,
never duplicating lines), Manager/Super Administrator finalized correction
extended to Dispatch Date and Recipient/Sales Category (previously lines/
notes only), permanent hard delete (never void/cancel/soft-delete — the
row is physically gone and every live calculation, which already reads
Dispatch/DispatchLine straight from the database, reflects that with no
manual stock patching), Dispatch History collapsed-by-default, the post-
finalize success screen (no more auto-redirect into History), and the
Dashboard Daily Activity row (Production card removed, Customers now means
unique recipients of a finalized dispatch on the selected date).

No stock formula, ledger/cutover, packaging rule, or Operator Daily
Figures behavior is touched by any of this — every assertion below relies
on the SAME shared stock_service/daily_figure_view() calculations already
used everywhere else in the app.
"""
import pathlib

import pytest

INDEX_HTML = (pathlib.Path(__file__).resolve().parent.parent / "static" / "dispatch.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (pathlib.Path(__file__).resolve().parent.parent / "static" / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10, carton_to_pieces=None):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    body = {"carton_to_pieces": carton_to_pieces} if carton_to_pieces else \
        {"cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces}
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json=body)
    return product


def _make_customer(client, category_id, name):
    return client.post("/api/admin/customers", json={
        "name": name, "sales_category_id": category_id, "confirm_not_duplicate": True,
    }).get_json()


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client, "Workflow Product")
    category = client.post("/api/admin/sales-categories", json={"name": "Workflow Category"}).get_json()
    customer_a = _make_customer(client, category["id"], "Workflow Recipient A")
    customer_b = _make_customer(client, category["id"], "Workflow Recipient B")
    return {"product": product, "category": category, "customer_a": customer_a, "customer_b": customer_b}


def _create_draft(client, number, date, customer_id, product_id, cartons, category_id=None):
    return client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "customer_id": customer_id,
        "sales_category_id": category_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()


def _issued_base_qty(client, product_id, date, shift="Day"):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()
    return row["issued"]["base_qty"]


def _closing_base_qty(client, product_id, date, shift="Day"):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()
    return row["closing"]["base_qty"]


def _set_opening(client, product_id, date, cartons, shift="Day"):
    client.post("/api/daily-figures", json={
        "product_id": product_id, "date": date, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })


# =====================================================================
# SECTION 27 — OPERATOR DRAFT EDITING
# =====================================================================

def test_operator_creates_draft_and_it_does_not_affect_issued(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-1", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                       setup["category"]["id"])
    assert d["status"] == "draft"
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0


def test_operator_can_reopen_and_change_date_on_own_draft(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-2", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    res = client.patch(f"/api/dispatches/{d['id']}", json={"date": "2026-08-02"})
    assert res.status_code == 200
    assert res.get_json()["date"] == "2026-08-02"


def test_operator_can_change_recipient_on_own_draft(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-3", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    res = client.patch(f"/api/dispatches/{d['id']}", json={
        "customer_id": setup["customer_b"]["id"], "sales_category_id": setup["category"]["id"],
    })
    assert res.status_code == 200
    assert res.get_json()["customer_id"] == setup["customer_b"]["id"]


def test_operator_can_change_notes_on_own_draft(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-4", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    res = client.patch(f"/api/dispatches/{d['id']}", json={"notes": "corrected note"})
    assert res.status_code == 200
    assert res.get_json()["notes"] == "corrected note"


def test_operator_can_change_product_and_quantity_via_replace_lines(client, setup, super_admin, login_as):
    other_product = _make_product(client, "Workflow Product Two")  # created as super_admin, before switching roles
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-5", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 2,
                       setup["category"]["id"])
    res = client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": other_product["id"], "cartons": 7, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["lines"]) == 1
    assert data["lines"][0]["product_id"] == other_product["id"]
    assert data["lines"][0]["cartons"] == 7


def test_resaving_draft_lines_does_not_duplicate(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-6", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 2,
                       setup["category"]["id"])
    for cartons in (3, 4, 4, 5):
        res = client.put(f"/api/dispatches/{d['id']}/lines", json={
            "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
        })
        assert res.status_code == 200
        assert len(res.get_json()["lines"]) == 1  # never grows regardless of how many times it's re-saved


def test_draft_remains_excluded_from_stock_after_edits(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-7", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 2,
                       setup["category"]["id"])
    client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}],
    })
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0


def test_operator_can_finalize_edited_draft_exactly_once(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-8", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 2,
                       setup["category"]["id"])
    client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 6, "packs": 0, "pieces": 0}],
    })
    fin = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert fin.status_code == 200
    assert fin.get_json()["status"] == "finalized"
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 600  # 6 Ctns, not 2+6=8 or double


def test_operator_loses_access_to_own_dispatch_once_finalized(client, setup, login_as):
    # Once finalized, can_edit() no longer treats the operator as the
    # owner of an editable draft — matching the existing rule every other
    # per-line endpoint (add/update/remove) already enforces.
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-9", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_replace_lines_rejects_a_finalized_dispatch_for_an_elevated_role_too(client, setup, super_admin):
    d = _create_finalized(client, "WF-9b", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    res = client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 409


def test_operator_cannot_replace_lines_on_another_operators_draft(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-10", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    client.post("/api/logout")
    login_as("op2", "password123", "operator")
    res = client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_manager_can_replace_lines_on_any_operators_draft(client, setup, login_as):
    login_as("op1", "password123", "operator")
    d = _create_draft(client, "WF-11", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    res = client.put(f"/api/dispatches/{d['id']}/lines", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


# =====================================================================
# SECTION 28 — FINALIZED EDIT / DATE CORRECTION / RECIPIENT CORRECTION
# =====================================================================

def _create_finalized(client, number, date, customer_id, product_id, cartons, category_id):
    d = _create_draft(client, number, date, customer_id, product_id, cartons, category_id)
    client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


def test_manager_can_edit_finalized_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-20", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fixing quantity", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["status"] == "finalized"


def test_super_admin_can_edit_finalized_dispatch(client, setup, super_admin):
    d = _create_finalized(client, "WF-21", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fixing quantity", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_correct_finalized_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-22", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "trying anyway", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_manager_can_correct_dispatch_date(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-23", "2026-08-08", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong date entered", "notes": None, "date": "2026-08-07",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["date"] == "2026-08-07"


def test_super_admin_can_correct_dispatch_date(client, setup, super_admin):
    d = _create_finalized(client, "WF-24", "2026-08-08", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong date entered", "notes": None, "date": "2026-08-07",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["date"] == "2026-08-07"


def test_correcting_date_moves_issued_between_dates_exactly_once(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-25", "2026-08-08", setup["customer_a"]["id"], setup["product"]["id"], 10,
                           setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-08") == 1000
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-07") == 0

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong date entered", "notes": None, "date": "2026-08-07",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 10, "packs": 0, "pieces": 0}],
    })
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-08") == 0
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-07") == 1000  # exactly once, not lost or doubled


def test_manager_can_change_recipient_via_correct(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-26", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong recipient", "notes": None,
        "customer_id": setup["customer_b"]["id"], "sales_category_id": setup["category"]["id"],
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["customer_id"] == setup["customer_b"]["id"]


def test_manager_can_change_product_via_correct(client, setup, super_admin, login_as):
    other_product = _make_product(client, "Workflow Product Three")  # created as super_admin, before switching roles
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-27", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong product", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": other_product["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch"]["lines"][0]["product_id"] == other_product["id"]
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0
    assert _issued_base_qty(client, other_product["id"], "2026-08-01") == 500


def test_correcting_quantity_replaces_old_contribution_exactly_once(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-28", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 10,
                           setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 1000

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "over-counted", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 8, "packs": 0, "pieces": 0}],
    })
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 800  # replaced, not 1000+800 or 1000-800


def test_correction_does_not_create_a_duplicate_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-29", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    before = client.get("/api/dispatches?limit=200").get_json()["total"]
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "notes": None, "date": "2026-08-02",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    after = client.get("/api/dispatches?limit=200").get_json()["total"]
    assert after == before


def test_following_closing_stock_reconciles_after_correction(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _set_opening(client, setup["product"]["id"], "2026-08-01", 100)
    d = _create_finalized(client, "WF-30", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 10,
                           setup["category"]["id"])
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-01") == 9000  # (100-10)*100 base units

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix qty", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-01") == 9600  # (100-4)*100


def test_dashboard_dispatch_count_moves_with_date_correction(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-31", "2026-08-08", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-08").get_json()["activity"]["dispatch"]["finalized"] == 1
    assert client.get("/api/dashboard?date=2026-08-07").get_json()["activity"]["dispatch"]["finalized"] == 0

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-07",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert client.get("/api/dashboard?date=2026-08-08").get_json()["activity"]["dispatch"]["finalized"] == 0
    assert client.get("/api/dashboard?date=2026-08-07").get_json()["activity"]["dispatch"]["finalized"] == 1


def test_dashboard_customer_count_moves_with_date_correction(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-32", "2026-08-08", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-08").get_json()["unique_recipients_today"] == 1
    assert client.get("/api/dashboard?date=2026-08-07").get_json()["unique_recipients_today"] == 0

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-07",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert client.get("/api/dashboard?date=2026-08-08").get_json()["unique_recipients_today"] == 0
    assert client.get("/api/dashboard?date=2026-08-07").get_json()["unique_recipients_today"] == 1


# =====================================================================
# SECTION 29 — PERMANENT DELETE
# =====================================================================

def test_manager_can_permanently_delete_finalized_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-40", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "entered by mistake", "confirm": True})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_super_admin_can_permanently_delete_finalized_dispatch(client, setup, super_admin):
    d = _create_finalized(client, "WF-41", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "entered by mistake", "confirm": True})
    assert res.status_code == 200


def test_operator_cannot_permanently_delete(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-42", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "trying anyway", "confirm": True})
    assert res.status_code == 403


def test_viewer_cannot_permanently_delete(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-43", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "trying anyway", "confirm": True})
    assert res.status_code == 403


def test_delete_requires_reason(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-44", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    res = client.delete(f"/api/dispatches/{d['id']}", json={"confirm": True})
    assert res.status_code == 400


def test_delete_requires_confirmation(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-45", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "no confirm flag"})
    assert res.status_code == 400


def test_deleted_dispatch_row_and_lines_physically_disappear(client, setup, login_as, app):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-46", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    line_id = d["lines"][0]["id"]
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.dispatch import Dispatch, DispatchLine
        assert _db.session.get(Dispatch, d["id"]) is None
        assert _db.session.get(DispatchLine, line_id) is None


def test_delete_does_not_delete_recipient_product_or_user(client, setup, login_as, app):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-47", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})

    customers = client.get("/api/admin/customers").get_json()
    assert any(c["id"] == setup["customer_a"]["id"] for c in customers)
    products = client.get("/api/admin/products").get_json()
    assert any(p["id"] == setup["product"]["id"] for p in products)
    with app.app_context():
        from webapp.models.user import User
        assert User.query.filter_by(username="mgr1").first() is not None


def test_deleted_dispatch_absent_from_history_list(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-48", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    results = client.get("/api/dispatches?limit=200").get_json()["results"]
    assert d["id"] not in [r["id"] for r in results]


def test_deleted_dispatch_no_longer_contributes_to_issued(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-49", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                           setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 500
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0


def test_deleted_dispatch_absent_from_dashboard(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-50", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    before = client.get("/api/dashboard?date=2026-08-01").get_json()
    assert d["id"] in [r["id"] for r in before["recent_dispatches"]]
    assert before["activity"]["dispatch"]["finalized"] == 1

    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    after = client.get("/api/dashboard?date=2026-08-01").get_json()
    assert d["id"] not in [r["id"] for r in after["recent_dispatches"]]
    assert after["activity"]["dispatch"]["finalized"] == 0


def test_customer_count_recalculates_after_delete(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-51", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["unique_recipients_today"] == 1
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["unique_recipients_today"] == 0


def test_closing_stock_recalculates_after_delete(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _set_opening(client, setup["product"]["id"], "2026-08-01", 100)
    d = _create_finalized(client, "WF-52", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 10,
                           setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 1000
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-01") == 9000  # (100-10)*100

    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-01") == 10000  # back to Opening, 100*100


def test_following_periods_opening_recalculates_after_delete(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _set_opening(client, setup["product"]["id"], "2026-08-01", 100)
    d = _create_finalized(client, "WF-53", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 10,
                           setup["category"]["id"])
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-02") == 9000  # carried forward from 08-01's 90

    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    assert _closing_base_qty(client, setup["product"]["id"], "2026-08-02") == 10000  # carried forward from 08-01's restored 100


def test_deleting_draft_does_not_alter_stock(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_draft(client, "WF-54", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                       setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "wrong entry", "confirm": True})
    assert res.status_code == 200
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0


def test_repeating_deletion_of_already_deleted_id_fails_safely(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-55", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    first = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    assert first.status_code == 200
    second = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone again", "confirm": True})
    assert second.status_code == 404


def test_no_stock_subtraction_happens_twice_across_two_deletes(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d1 = _create_finalized(client, "WF-56", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 5,
                            setup["category"]["id"])
    d2 = _create_finalized(client, "WF-57", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 3,
                            setup["category"]["id"])
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 800
    client.delete(f"/api/dispatches/{d1['id']}", json={"reason": "gone", "confirm": True})
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 300
    client.delete(f"/api/dispatches/{d2['id']}", json={"reason": "gone", "confirm": True})
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 0


def test_delete_transaction_rolls_back_on_failure(client, setup, login_as, monkeypatch):
    """A failure between the delete-flush and the final commit must leave
    nothing persisted. In production this is guaranteed automatically —
    each HTTP request gets its own fresh app-context/session lifecycle, so
    an unhandled exception's teardown rolls back whatever that request
    flushed before a later, separate request ever runs. This test's `app`
    fixture instead holds ONE app-context open for the whole test, so that
    automatic per-request teardown doesn't fire between these two calls
    the way it would for two real, separate requests — so the rollback
    that would happen naturally at that request boundary is invoked
    explicitly here instead (in the SAME already-active app context the
    fixture provides — pushing a second, nested one here disturbs the
    scoped session and defeats the check), to verify the same guarantee:
    nothing the failed request flushed was ever committed."""
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-58", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])

    import webapp.routes.dispatches as route_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure before commit")
    monkeypatch.setattr(route_module, "record_audit", _boom)

    with pytest.raises(RuntimeError):
        client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})

    from webapp.extensions import db as _db
    _db.session.rollback()

    still_there = client.get(f"/api/dispatches/{d['id']}")
    assert still_there.status_code == 200
    assert still_there.get_json()["status"] == "finalized"


def test_audit_log_survives_hard_delete_with_snapshot(client, setup, login_as, app):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-59", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "recorded in error", "confirm": True})

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="permanent_delete_dispatch", entity_id=str(d["id"])).first()
        assert entry is not None
        before = __import__("json").loads(entry.before_json)
        assert before["operation"] == "permanent_delete_dispatch"
        assert before["deletion_reason"] == "recorded in error"
        assert before["dispatch_number"] == "WF-59"


def test_audit_log_entry_does_not_count_as_operational_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_finalized(client, "WF-60", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                           setup["category"]["id"])
    client.delete(f"/api/dispatches/{d['id']}", json={"reason": "gone", "confirm": True})
    results = client.get("/api/dispatches?limit=200").get_json()["results"]
    assert d["id"] not in [r["id"] for r in results]
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["activity"]["dispatch"]["finalized"] == 0


# =====================================================================
# SECTION 30 — HISTORY COLLAPSE (frontend markup)
# =====================================================================

def test_date_groups_collapsed_by_default_markup():
    idx = INDEX_HTML.index("async function loadList(){")
    end = INDEX_HTML.index("\nasync function loadUserFilterOptions", idx)
    body = INDEX_HTML[idx:end]
    assert '<div class="date-group collapsed">' in body
    # nothing special-cases "today" or the newest date into staying open
    assert "todayStr" not in body


def test_toggle_group_click_handler_present():
    assert "data-toggle-group" in INDEX_HTML
    assert "classList.toggle('collapsed')" in INDEX_HTML


def test_draft_and_finalized_counts_shown_in_group_summary():
    idx = INDEX_HTML.index("async function loadList(){")
    end = INDEX_HTML.index("\nasync function loadUserFilterOptions", idx)
    body = INDEX_HTML[idx:end]
    assert "finalizedCount" in body
    assert "draftCount" in body


def test_search_and_filters_still_present_and_unremoved():
    for fid in ["fDate", "fDateFrom", "fDateTo", "fCustomer", "fProduct", "fNumber", "fInvoice", "fStatus"]:
        assert f'id="{fid}"' in INDEX_HTML


def test_edit_draft_and_manager_actions_present_in_detail_actions():
    idx = INDEX_HTML.index("const actions = document.getElementById('detailActions');")
    end = INDEX_HTML.index("\n// ---------- Correct Record", INDEX_HTML.index("document.getElementById('backToListBtn')"))
    body = INDEX_HTML[idx:end]
    assert 'data-action="edit-draft"' in body
    assert 'data-action="correct"' in body
    assert 'data-action="delete"' in body


# =====================================================================
# SECTION 31 — POST-FINALIZE WORKFLOW (frontend markup)
# =====================================================================

def test_success_screen_markup_present():
    assert 'id="tab-success"' in INDEX_HTML
    assert "Dispatch Finalized" in INDEX_HTML
    assert 'id="successAddNewBtn"' in INDEX_HTML
    assert 'id="successViewHistoryBtn"' in INDEX_HTML


def test_finalize_from_new_dispatch_form_does_not_auto_redirect_to_history():
    idx = INDEX_HTML.index("async function saveNewDispatch(")
    end = INDEX_HTML.index("\n// Draft editing", idx)
    body = INDEX_HTML[idx:end]
    assert "showFinalizeSuccess(" in body
    # the finalize branch calls showFinalizeSuccess() and returns immediately
    # after — it must never fall through to the plain draft-save tail
    # (toast('Draft saved.'); resetForm(); switchTab('list');) below it.
    finalize_idx = body.index("if(finalize){")
    success_idx = body.index("showFinalizeSuccess(", finalize_idx)
    next_return_idx = body.index("return;", success_idx)
    assert "switchTab('list')" not in body[finalize_idx:next_return_idx]


def test_add_new_dispatch_preserves_working_date():
    idx = INDEX_HTML.index("document.getElementById('successAddNewBtn')")
    body = INDEX_HTML[idx:idx+300]
    assert "resetForm(lastFinalizedDate)" in body
    assert "switchTab('new')" in body


def test_double_finalize_does_not_duplicate_dispatch(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    d = _create_draft(client, "WF-61", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1,
                       setup["category"]["id"])
    first = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert first.status_code == 200
    second = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert second.status_code == 400  # already finalized — the existing status guard
    assert _issued_base_qty(client, setup["product"]["id"], "2026-08-01") == 100  # counted once


# =====================================================================
# SECTION 32 — DASHBOARD
# =====================================================================

def test_production_card_removed_from_daily_activity_markup():
    idx = DASHBOARD_HTML.index('<h2 id="activityTitle">')
    end = DASHBOARD_HTML.index("</div>\n      <p class=\"last-updated\" id=\"lastActivityAt\"")
    body = DASHBOARD_HTML[idx:end]
    assert "productionFinalizedStat" not in body
    assert "productionDraftStat" not in body
    assert "dispatchFinalizedStat" in body
    assert "returnsFinalizedStat" in body
    assert "activeCustomersStat" in body


def test_production_still_used_elsewhere_in_dashboard_backend():
    # The card is gone from the frontend Daily Activity row, but the
    # backend still computes and returns Production data for every other
    # consumer (detailed sections, reports, ledger calculations).
    import inspect
    from webapp.services import dashboard_service
    source = inspect.getsource(dashboard_service)
    assert "production_by_shift" in source
    assert "_production_by_shift" in source


def test_production_activity_still_computed_by_backend(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    data = client.get("/api/dashboard?date=2026-08-01").get_json()
    assert data["activity"]["production"]["day_finalized"] == 1
    assert str(setup["product"]["id"]) in data["production_by_shift"]


def test_customers_counts_unique_recipients_not_dispatch_count(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _create_finalized(client, "WF-70", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-71", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-72", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-73", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-74", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-75", "2026-08-01", setup["customer_b"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-76", "2026-08-01", setup["customer_b"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    third = _make_customer(client, setup["category"]["id"], "Workflow Recipient C")
    _create_finalized(client, "WF-77", "2026-08-01", third["id"], setup["product"]["id"], 1, setup["category"]["id"])

    data = client.get("/api/dashboard?date=2026-08-01").get_json()
    assert data["activity"]["dispatch"]["finalized"] == 8
    assert data["unique_recipients_today"] == 3


def test_draft_recipient_does_not_count(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _create_draft(client, "WF-78", "2026-08-01", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["unique_recipients_today"] == 0


def test_another_date_does_not_leak_into_current_date(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    _create_finalized(client, "WF-79", "2026-08-02", setup["customer_a"]["id"], setup["product"]["id"], 1, setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["unique_recipients_today"] == 0
    assert client.get("/api/dashboard?date=2026-08-02").get_json()["unique_recipients_today"] == 1


def test_different_recipients_with_identical_display_names_counted_by_id(client, setup, login_as):
    login_as("mgr1", "password123", "manager")
    twin_a = _make_customer(client, setup["category"]["id"], "Same Name Recipient")
    twin_b = client.post("/api/admin/customers", json={
        "name": "Same Name Recipient", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()
    assert twin_a["id"] != twin_b["id"]
    _create_finalized(client, "WF-80", "2026-08-01", twin_a["id"], setup["product"]["id"], 1, setup["category"]["id"])
    _create_finalized(client, "WF-81", "2026-08-01", twin_b["id"], setup["product"]["id"], 1, setup["category"]["id"])
    assert client.get("/api/dashboard?date=2026-08-01").get_json()["unique_recipients_today"] == 2


def test_active_customers_lifetime_total_unaffected(client, setup, login_as):
    """The unrelated, pre-existing lifetime active_customers field must
    remain exactly as it was — this round only adds a new field for the
    Daily Activity card, never repurposes or removes the old one."""
    login_as("mgr1", "password123", "manager")
    before = client.get("/api/dashboard?date=2026-08-01").get_json()["active_customers"]
    _make_customer(client, setup["category"]["id"], "Another Recipient Entirely")
    after = client.get("/api/dashboard?date=2026-08-01").get_json()["active_customers"]
    assert after == before + 1
