"""Accountant gains real working access to Spare Parts Stock In/Stock Out
ONLY (nav + page + API), reusing the existing Operator/Manager/Super Admin
movement business logic unchanged. Adjustment, stock count, opening
balance, void, Planning, Master Data, Admin, and all financial data stay
exactly as restrictive as before (see test_spare_parts_role_navigation.py
for the broader financial-data-boundary coverage this extends).
"""
from pathlib import Path

from webapp.extensions import db
from webapp.services import spare_part_movement_service as movement_svc

from tests.test_spare_parts import (
    _create_spare_part_via_manager,
    _get_or_create_department,
    _login_spare_parts,
    _stock_out,
)
from tests.test_spare_parts_role_navigation import _assert_no_financial_keys

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SPARE_PARTS_SHELL_JS = (STATIC_DIR / "spare-parts-shell.js").read_text(encoding="utf-8")


# ---------- source-level nav ----------

def test_accountant_added_to_stock_in_and_stock_out_nav_only():
    assert (
        "{ key: 'stock-in', label: 'Stock In', href: '/spare-parts-stock-in.html', "
        "roles: ['operator', 'manager', 'accountant', 'super_admin'] }"
    ) in SPARE_PARTS_SHELL_JS
    assert (
        "{ key: 'stock-out', label: 'Stock Out', href: '/spare-parts-stock-out.html', "
        "roles: ['operator', 'manager', 'accountant', 'super_admin'] }"
    ) in SPARE_PARTS_SHELL_JS
    # Planning/Master Data/Admin must remain Super-Admin-only, unaffected.
    assert "roles: ['super_admin'] }" in SPARE_PARTS_SHELL_JS


# ---------- page-level access ----------

def test_accountant_reaches_stock_in_and_stock_out_pages(client, make_user):
    _login_spare_parts(client, make_user, "acctstock_page1", "accountant")
    assert client.get("/spare-parts-stock-in.html").status_code == 200
    assert client.get("/spare-parts-stock-out.html").status_code == 200


def test_accountant_still_denied_planning_master_admin_pages(client, make_user):
    _login_spare_parts(client, make_user, "acctstock_page2", "accountant")
    for page in ("/spare-parts-planning.html", "/spare-parts-master.html"):
        res = client.get(page)
        assert res.status_code == 302, page
        assert res.headers["Location"] == "/spare-parts.html", page
    assert client.get("/admin.html").status_code == 302


# ---------- Stock In: Accountant can actually record movements ----------

def test_accountant_can_record_stock_in_and_stock_changes_correctly(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockin1", "accountant")
    res = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10", "remarks": "accountant stock in",
    })
    assert res.status_code == 201, res.get_json()
    movement = res.get_json()
    assert movement["movement_type"] == "STOCK_IN"
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 10


def test_accountant_stock_in_creates_audit_record(app, client, make_user):
    from webapp.models.audit_log import AuditLog
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockin2", "accountant")
    res = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "5",
    })
    movement_id = res.get_json()["id"]
    with app.app_context():
        entry = AuditLog.query.filter_by(action="stock_in", entity_id=str(movement_id)).first()
        assert entry is not None
        assert entry.username == "acctstockin2"


def test_accountant_stock_in_validation_still_applies(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockin3", "accountant")
    res = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "-5",
    })
    assert res.status_code == 400


# ---------- Stock Out: Accountant can actually record movements ----------

def test_accountant_can_record_stock_out_and_stock_changes_correctly(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockout1", "accountant")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    res = _stock_out(app, client, part["id"], quantity="4", requested_by="Accountant Tester")
    assert res.status_code == 201, res.get_json()
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 6


def test_accountant_stock_out_creates_audit_record(app, client, make_user):
    from webapp.models.audit_log import AuditLog
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockout2", "accountant")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    res = _stock_out(app, client, part["id"], quantity="2")
    movement_id = res.get_json()["id"]
    with app.app_context():
        entry = AuditLog.query.filter_by(action="stock_out", entity_id=str(movement_id)).first()
        assert entry is not None
        assert entry.username == "acctstockout2"


def test_accountant_stock_out_cannot_exceed_available_stock(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockout3", "accountant")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = _stock_out(app, client, part["id"], quantity="6")
    assert res.status_code == 400
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 5


def test_accountant_stock_out_requires_department(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctstockout4", "accountant")
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x",
    })
    assert res.status_code == 400


# ---------- privilege containment: Accountant gains nothing else ----------

def test_accountant_still_cannot_call_adjustment_stock_count_opening_balance_void(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctcontain1", "accountant")
    assert client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "1", "direction": "in",
    }).status_code == 403
    assert client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "1", "reason": "test",
    }).status_code == 403
    assert client.post("/api/spare-parts/movements/opening-balance", json={
        "spare_part_id": part["id"], "quantity": "1",
    }).status_code == 403


def test_accountant_still_cannot_void_a_movement(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "voidsetupmgr", "manager")
    movement = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).get_json()
    _login_spare_parts(client, make_user, "acctcontain2", "accountant")
    res = client.post(f"/api/spare-parts/movements/{movement['id']}/void", json={"reason": "test"})
    assert res.status_code == 403


def test_accountant_cannot_write_or_see_pricing_in_spare_parts_responses(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "acctcontain3", "super_admin", password="password123")
    client.patch(f"/api/spare-parts/{part['id']}", json={
        "china_buying_price": "10.00", "local_buying_price": "12.00", "selling_price": "20.00",
    })
    other = app.test_client()
    _login_spare_parts(other, make_user, "acctcontain4", "accountant")
    assert other.patch(f"/api/spare-parts/{part['id']}", json={"china_buying_price": "99"}).status_code == 403
    detail = other.get(f"/api/spare-parts/{part['id']}")
    assert detail.status_code == 200
    _assert_no_financial_keys(detail.get_json(), "accountant:/api/spare-parts/{id}")
    listing = other.get("/api/spare-parts")
    assert listing.status_code == 200
    _assert_no_financial_keys(listing.get_json(), "accountant:/api/spare-parts")
    assert other.get("/api/spare-parts/planning").status_code == 403
    assert other.get("/api/admin/spare-parts/pricing").status_code == 403


# ---------- other roles unaffected ----------

def test_operator_stock_in_out_still_works(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "opstill1", "operator")
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).status_code == 201
    res = _stock_out(app, client, part["id"], quantity="1")
    assert res.status_code == 201


def test_manager_stock_in_out_still_works(app, client, make_user):
    _login_spare_parts(client, make_user, "mgrstill1", "manager")
    from tests.test_spare_parts import _create_spare_part
    part = _create_spare_part(client)
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).status_code == 201
    res = _stock_out(app, client, part["id"], quantity="1")
    assert res.status_code == 201


def test_super_admin_stock_in_out_still_works(app, client, make_user):
    _login_spare_parts(client, make_user, "sastill1", "super_admin")
    from tests.test_spare_parts import _create_spare_part
    part = _create_spare_part(client)
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).status_code == 201
    res = _stock_out(app, client, part["id"], quantity="1")
    assert res.status_code == 201


def test_viewer_still_cannot_stock_in_or_stock_out(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "viewerstill1", "viewer")
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).status_code == 403
    assert _stock_out(app, client, part["id"], quantity="1").status_code == 403
    assert client.get("/spare-parts-stock-in.html").status_code == 302
    assert client.get("/spare-parts-stock-out.html").status_code == 302
