"""
Spare Parts inventory — master data, movement ledger, role permissions,
and section isolation. See webapp/services/spare_part_movement_service.py
for the concurrency idiom (conditional atomic UPDATE, mirroring
correction_request_service.consume_grant()) and current-stock derivation.
"""
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.services import spare_part_movement_service as movement_svc
from webapp.services import spare_part_service as sp_service


def _login_spare_parts(client, make_user, username, role, password="password123"):
    make_user(username, password, role)
    with client.application.app_context():
        from webapp.models.section import Section
        from webapp.models.user import User
        user = User.query.filter_by(username=username).first()
        section = Section.query.filter_by(code="spare_parts").first()
        if section is None:
            sp_service_module = __import__(
                "webapp.services.section_access_service", fromlist=["ensure_seeded"]
            )
            sp_service_module.ensure_seeded()
            section = Section.query.filter_by(code="spare_parts").first()
        from webapp.models.section import UserSectionAccess
        db.session.add(UserSectionAccess(user_id=user.id, section_id=section.id))
        db.session.commit()
    res = client.post("/api/login", json={"username": username, "password": password, "section": "spare_parts"})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["user"]


_part_counter = [0]


def _create_spare_part(client, **overrides):
    _part_counter[0] += 1
    body = {"name": f"Bearing 6304-{_part_counter[0]}", "model": "6304", "size": "20mm", "unit": "pcs"}
    body.update(overrides)
    res = client.post("/api/spare-parts", json=body)
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def _create_spare_part_via_manager(app, make_user, **overrides):
    # Operator-logged-in tests need a pre-existing spare part to operate
    # on — Operator itself cannot create master data, so a separate
    # Manager session (own cookie jar) creates it first.
    other = app.test_client()
    _login_spare_parts(other, make_user, f"setupmgr{_part_counter[0] + 1}", "manager")
    return _create_spare_part(other, **overrides)


def _get_or_create_department(app, name="General"):
    with app.app_context():
        from webapp.models.operational_department import SparePartDepartment
        dept = SparePartDepartment.query.filter_by(name=name).first()
        if dept is None:
            dept = SparePartDepartment(name=name, active=True)
            db.session.add(dept)
            db.session.commit()
        return dept.id


def _stock_out(app, client, spare_part_id, **overrides):
    body = {"spare_part_id": spare_part_id, "quantity": "1", "requested_by": "x"}
    body["department_id"] = overrides.pop("department_id", None) or _get_or_create_department(app)
    body.update(overrides)
    return client.post("/api/spare-parts/movements/stock-out", json=body)


# ---------- master data ----------

def test_add_spare_part(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr1", "manager")
    part = _create_spare_part(client)
    assert part["name"].startswith("Bearing 6304")
    assert part["current_stock"] == "0"


def test_edit_spare_part(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr2", "manager")
    part = _create_spare_part(client)
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"model": "6304-2RS"})
    assert res.status_code == 200
    assert res.get_json()["model"] == "6304-2RS"


def test_deactivate_spare_part(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr3", "manager")
    part = _create_spare_part(client)
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"active": False})
    assert res.status_code == 200
    assert res.get_json()["active"] is False


def test_manager_cannot_permanently_delete_spare_part(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr4", "manager")
    part = _create_spare_part(client)
    res = client.delete(f"/api/spare-parts/{part['id']}")
    assert res.status_code == 403
    assert db.session.get(SparePart, part["id"]) is not None


def test_machine_association_works(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr5", "manager")
    machine = client.post("/api/spare-parts/machines", json={"name": "TP"}).get_json()
    part = _create_spare_part(client, machine_ids=[machine["id"]])
    assert part["machines"][0]["name"] == "TP"


def test_supplier_association_and_contacts_and_price_text_preserved(app):
    with app.app_context():
        from webapp.models.user import User
        actor = User.query.first()
        part = sp_service.create_spare_part(name="Contactor 30A")
        supplier = sp_service.find_or_create_supplier("Central Town", building="Ham near Energy Center",
                                                        contacts="256704127039")
        assert supplier.contacts == "256704127039"
        assert isinstance(supplier.contacts, str)
        from webapp.models.spare_part import SparePartSupplier
        db.session.add(SparePartSupplier(
            spare_part_id=part.id, supplier_id=supplier.id, raw_price_text="150,000shs",
        ))
        db.session.commit()
        quote = SparePartSupplier.query.filter_by(spare_part_id=part.id).first()
        assert quote.raw_price_text == "150,000shs"


def test_machine_case_variants_normalize_to_one_machine(app):
    with app.app_context():
        m1 = sp_service.normalize_machine("tp")
        m2 = sp_service.normalize_machine("TP")
        m3 = sp_service.normalize_machine("tP")
        assert m1.id == m2.id == m3.id


def test_distinct_machine_spellings_stay_separate(app):
    with app.app_context():
        m1 = sp_service.normalize_machine("Napkin")
        m2 = sp_service.normalize_machine("nap")
        assert m1.id != m2.id


# ---------- movements ----------

def test_stock_in_increases_stock(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op1", "operator")
    res = client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    assert res.status_code == 201
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 10


def test_stock_out_decreases_stock(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op2", "operator")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    res = _stock_out(app, client, part["id"], quantity="4", requested_by="Ambrose")
    assert res.status_code == 201
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 6


def test_adjustment_in_increases_stock(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr6", "manager")
    part = _create_spare_part(client)
    res = client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "5", "direction": "in", "reason": "count correction",
    })
    assert res.status_code == 201
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 5


def test_adjustment_out_decreases_stock(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr7", "manager")
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "5", "direction": "in",
    })
    res = client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "2", "direction": "out",
    })
    assert res.status_code == 201
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 3


def test_void_neutralizes_movement(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr8", "manager")
    part = _create_spare_part(client)
    movement = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "10",
    }).get_json()
    res = client.post(f"/api/spare-parts/movements/{movement['id']}/void", json={"reason": "wrong entry"})
    assert res.status_code == 200
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 0


def test_current_stock_reconciles_cache_to_ledger(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr9", "manager")
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    _stock_out(app, client, part["id"], quantity="3")
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "1", "direction": "out",
    })
    with app.app_context():
        from webapp.models.spare_part import SparePart as SP
        spare = db.session.get(SP, part["id"])
        ledger_stock = movement_svc.current_stock(part["id"])
        assert spare.current_stock_cache == ledger_stock == 6


def test_stock_out_cannot_exceed_available_stock(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op3", "operator")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = _stock_out(app, client, part["id"], quantity="6")
    assert res.status_code == 400
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 5


def test_negative_stock_prevented_at_zero(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op4", "operator")
    res = _stock_out(app, client, part["id"])
    assert res.status_code == 400


def test_decimal_quantities_no_float_drift(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr10", "manager")
    part = _create_spare_part(client, unit="metres")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "0.1"})
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "0.2"})
    with app.app_context():
        from decimal import Decimal
        assert movement_svc.current_stock(part["id"]) == Decimal("0.3")


def test_units_display_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr11", "manager")
    part = _create_spare_part(client, unit="litres")
    assert part["unit"] == "litres"


def test_requested_by_persists(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op5", "operator")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    movement = _stock_out(app, client, part["id"], requested_by="Juma").get_json()
    assert movement["requested_by"] == "Juma"


def test_approved_by_persists(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op6", "operator")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    movement = _stock_out(app, client, part["id"], requested_by="Juma", approved_by="Manager Bob").get_json()
    assert movement["approved_by"] == "Manager Bob"


def test_entered_by_is_automatic(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    user = _login_spare_parts(client, make_user, "op7", "operator")
    movement = client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "5",
    }).get_json()
    assert movement["entered_by_user_id"] == user["id"]


# ---------- low stock ----------

def test_low_stock_threshold_triggers(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr12", "manager")
    part = _create_spare_part(client, minimum_stock="5")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "3"})
    with app.app_context():
        from webapp.models.spare_part import SparePart as SP
        spare = db.session.get(SP, part["id"])
        assert movement_svc.is_low_stock(spare) is True


def test_low_stock_not_flagged_without_threshold(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr13", "manager")
    part = _create_spare_part(client)
    with app.app_context():
        from webapp.models.spare_part import SparePart as SP
        spare = db.session.get(SP, part["id"])
        assert movement_svc.is_low_stock(spare) is False


# ---------- roles ----------

def test_viewer_cannot_write(app, client, make_user):
    _login_spare_parts(client, make_user, "view1", "viewer")
    res = client.post("/api/spare-parts", json={"name": "x"})
    assert res.status_code == 403


def test_operator_can_stock_in_out_but_not_master_data(app, client, make_user):
    part = _create_spare_part_via_manager(app, make_user)
    _login_spare_parts(client, make_user, "op8", "operator")
    assert client.post("/api/spare-parts", json={"name": "y"}).status_code == 403
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "1",
    }).status_code == 201


def test_accountant_read_only(app, client, make_user):
    _login_spare_parts(client, make_user, "acct1", "accountant")
    assert client.get("/api/spare-parts").status_code == 200
    assert client.post("/api/spare-parts", json={"name": "x"}).status_code == 403
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": 1, "quantity": "1",
    }).status_code == 403


def test_manager_can_manage_master_data(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr14", "manager")
    assert _create_spare_part(client, name="Allen Bolt M6")["active"] is True


def test_super_admin_full_control(app, client, make_user):
    _login_spare_parts(client, make_user, "spsa1", "super_admin")
    part = _create_spare_part(client, name="Gasket")
    assert client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "1",
    }).status_code == 201


# ---------- section isolation ----------

def test_spare_parts_api_inaccessible_from_finished_goods_session(client, login_as):
    login_as("fguser1", "password123", "manager")  # defaults to finished_goods
    res = client.get("/api/spare-parts")
    assert res.status_code == 403
    assert res.get_json()["error"] == "forbidden_section"


def test_finished_goods_api_inaccessible_from_spare_parts_session(app, client, make_user):
    _login_spare_parts(client, make_user, "spuser1", "manager")
    res = client.get("/api/dashboard")
    assert res.status_code == 403
    assert res.get_json()["error"] == "forbidden_section"


def test_dashboard_summary_reports_low_stock_item(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr15", "manager")
    part = _create_spare_part(client, minimum_stock="100")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "1"})
    res = client.get("/api/spare-parts/dashboard")
    data = res.get_json()
    assert data["total_active_spare_parts"] >= 1
    assert any(row["name"] == part["name"] for row in data["low_stock"])
