"""
Spare Parts — targeted UX/master-data refinement round: Stock In
simplification, Stock Out Department/Machine, Requested/Approved-By
suggestions, Specifications, Categories, operational Departments,
restricted Buying/Selling pricing + profit/margin, duplicate detection,
and selector-based Adjustments/Opening Balance. See
tests/test_spare_parts.py for the base movement-ledger/role tests this
round builds on (unchanged by this round).
"""
from decimal import Decimal

from webapp.extensions import db
from webapp.services import spare_part_service as sp_service

from tests.test_spare_parts import _login_spare_parts


def _create_department(app, name="Weaving"):
    with app.app_context():
        from webapp.models.operational_department import SparePartDepartment
        dept = SparePartDepartment.query.filter_by(name=name).first()
        if dept is None:
            dept = SparePartDepartment(name=name, active=True)
            db.session.add(dept)
            db.session.commit()
        return dept.id


def _create_spare_part(client, **overrides):
    body = {"name": "Refinement Spare", "specifications": "20mm", "unit": "pcs"}
    body.update(overrides)
    res = client.post("/api/spare-parts", json=body)
    assert res.status_code == 201, res.get_json()
    return res.get_json()


# ---------- Stock In simplified ----------

def test_stock_in_minimal_fields_still_increase_stock(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr1", "manager")
    part = _create_spare_part(client)
    res = client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    assert res.status_code == 201
    from webapp.services import spare_part_movement_service as movement_svc
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 5


# ---------- Stock Out: Department required, Machine optional ----------

def test_stock_out_requires_department(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr2", "manager")
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x",
    })
    assert res.status_code == 400


def test_stock_out_machine_is_optional(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr3", "manager")
    dept_id = _create_department(app)
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x", "department_id": dept_id,
    })
    assert res.status_code == 201
    assert res.get_json()["machine_id"] is None


def test_stock_out_with_machine_matching_department(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr4", "manager")
    dept_id = _create_department(app, "Dyeing")
    machine = client.post("/api/spare-parts/machines", json={"name": "Loom 1", "department_id": dept_id}).get_json()
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x",
        "department_id": dept_id, "machine_id": machine["id"],
    })
    assert res.status_code == 201
    assert res.get_json()["machine_id"] == machine["id"]


def test_machine_filtering_by_department(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr5", "manager")
    dept_a = _create_department(app, "DeptA")
    dept_b = _create_department(app, "DeptB")
    m_a = client.post("/api/spare-parts/machines", json={"name": "MachineA", "department_id": dept_a}).get_json()
    client.post("/api/spare-parts/machines", json={"name": "MachineB", "department_id": dept_b})
    res = client.get(f"/api/spare-parts/machines?department_id={dept_a}")
    names = [m["name"] for m in res.get_json()]
    assert "MachineA" in names
    assert "MachineB" not in names


def test_negative_stock_still_impossible_with_department(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr6", "manager")
    dept_id = _create_department(app)
    part = _create_spare_part(client)
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x", "department_id": dept_id,
    })
    assert res.status_code == 400


# ---------- Requested By / Approved By suggestions ----------

def test_people_suggestions_includes_active_usernames(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_user_suggest", "manager")
    res = client.get("/api/spare-parts/movements/people")
    assert res.status_code == 200
    assert "ref_user_suggest" in res.get_json()


def test_people_suggestions_includes_historical_requested_by(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr7", "manager")
    dept_id = _create_department(app)
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "HistoricalNameXYZ", "department_id": dept_id,
    })
    res = client.get("/api/spare-parts/movements/people?q=Historical")
    assert "HistoricalNameXYZ" in res.get_json()


def test_free_text_fallback_remains_valid_for_requested_by(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr8", "manager")
    dept_id = _create_department(app)
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "BrandNewNameNeverSeen", "department_id": dept_id,
    })
    assert res.status_code == 201


# ---------- Spare master fields ----------

def test_specifications_field_works(client, make_user, app):
    _login_spare_parts(client, make_user, "ref_mgr9", "manager")
    part = _create_spare_part(client, name="Spec Test", specifications="Digital - Medium")
    assert part["specifications"] == "Digital - Medium"


def test_minimum_stock_and_unit_still_work(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr10", "manager")
    part = _create_spare_part(client, name="Unit Test", unit="litres", minimum_stock="5")
    assert part["unit"] == "litres"
    assert part["minimum_stock"] == "5.000"


def test_category_assignment_works(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr11", "manager")
    category = client.post("/api/spare-parts/categories", json={"name": "Electrical"}).get_json()
    part = _create_spare_part(client, name="Categorized Spare", category_id=category["id"])
    assert part["category_id"] == category["id"]


def test_category_filter_on_list(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr12", "manager")
    category = client.post("/api/spare-parts/categories", json={"name": "Mechanical"}).get_json()
    _create_spare_part(client, name="In Category", category_id=category["id"])
    _create_spare_part(client, name="No Category")
    res = client.get(f"/api/spare-parts?category_id={category['id']}")
    names = [p["name"] for p in res.get_json()]
    assert "In Category" in names
    assert "No Category" not in names


def test_uncategorized_legacy_records_remain_usable(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr13", "manager")
    part = _create_spare_part(client, name="Legacy Uncategorized")
    assert part["category_id"] is None
    res = client.get("/api/spare-parts")
    assert res.status_code == 200


def test_duplicate_warning_triggers_on_probable_match(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr14", "manager")
    _create_spare_part(client, name="Duplicate Me", specifications="20mm")
    res = client.post("/api/spare-parts", json={"name": "Duplicate Me", "specifications": "20mm"})
    assert res.status_code == 409
    assert res.get_json()["error"] == "possible_duplicate"


def test_duplicate_confirm_allows_creation(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr15", "manager")
    _create_spare_part(client, name="Confirm Dup", specifications="20mm")
    res = client.post("/api/spare-parts", json={
        "name": "Confirm Dup", "specifications": "20mm", "confirm_duplicate": True,
    })
    assert res.status_code == 201


def test_different_specifications_not_flagged_as_duplicate(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr16", "manager")
    _create_spare_part(client, name="Shared Name", specifications="20mm")
    res = client.post("/api/spare-parts", json={"name": "Shared Name", "specifications": "30mm"})
    assert res.status_code == 201


# ---------- Machines <-> Departments ----------

def test_machine_links_to_department(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr17", "manager")
    dept_id = _create_department(app, "Packing")
    machine = client.post("/api/spare-parts/machines", json={"name": "Packer 1", "department_id": dept_id}).get_json()
    assert machine["department_id"] == dept_id


def test_machine_aliases_remain_intact_after_department_added(app):
    with app.app_context():
        m1 = sp_service.normalize_machine("tp")
        m2 = sp_service.normalize_machine("TP")
        assert m1.id == m2.id
        from webapp.models.machine import MachineAlias
        # "tp" and "TP" normalize to the same normalized_text, so the
        # second call reuses the first alias row rather than creating a
        # second one for the differently-cased raw text (normalized_text
        # is the unique identity key — see MachineAlias's docstring).
        aliases = MachineAlias.query.filter_by(machine_id=m1.id).all()
        assert len(aliases) == 1


def test_operational_department_does_not_interfere_with_store_section(app, client, make_user):
    # The Spare-Parts-only operational department must be a completely
    # separate concept/table from the Store Department application
    # section — confirm the login/session flow for the Spare Parts
    # SECTION still works normally after operational departments exist.
    _create_department(app, "SomeDept")
    user = _login_spare_parts(client, make_user, "ref_mgr18", "manager")
    assert client.get("/api/session").get_json()["active_section"] == "spare_parts"


# ---------- Pricing ----------

def test_buying_price_stored_exactly(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr19", "manager")
    part = _create_spare_part(client, name="Priced A", buying_price="12345.67")
    assert part["buying_price"] == "12345.67"


def test_selling_price_stored_exactly(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr20", "manager")
    part = _create_spare_part(client, name="Priced B", selling_price="99999.99")
    assert part["selling_price"] == "99999.99"


def test_profit_calculation_correct(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr21", "manager")
    part = _create_spare_part(client, name="Priced C", buying_price="100", selling_price="150")
    assert Decimal(part["profit"]) == Decimal("50")


def test_margin_calculation_correct(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr22", "manager")
    part = _create_spare_part(client, name="Priced D", buying_price="100", selling_price="200")
    assert Decimal(part["margin_percent"]) == Decimal("50")


def test_null_prices_safe(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr23", "manager")
    part = _create_spare_part(client, name="Priced E")
    assert part["buying_price"] is None
    assert part["profit"] is None
    assert part["margin_percent"] is None


def test_zero_selling_price_safe(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr24", "manager")
    part = _create_spare_part(client, name="Priced F", buying_price="50", selling_price="0")
    assert Decimal(part["profit"]) == Decimal("-50")
    assert part["margin_percent"] is None  # division by zero avoided, never fabricated


def test_normal_users_cannot_see_pricing(app, client, make_user):
    other = app.test_client()
    _login_spare_parts(other, make_user, "ref_mgr25", "manager")
    part = _create_spare_part(other, name="Restricted", buying_price="10", selling_price="20")

    _login_spare_parts(client, make_user, "ref_viewer1", "viewer")
    res = client.get(f"/api/spare-parts/{part['id']}")
    data = res.get_json()
    assert "buying_price" not in data
    assert "selling_price" not in data
    assert "profit" not in data
    assert "margin_percent" not in data


def test_pricing_absent_from_list_for_operator(app, client, make_user):
    other = app.test_client()
    _login_spare_parts(other, make_user, "ref_mgr26", "manager")
    _create_spare_part(other, name="Restricted List", buying_price="10", selling_price="20")

    _login_spare_parts(client, make_user, "ref_op1", "operator")
    res = client.get("/api/spare-parts")
    for p in res.get_json():
        assert "buying_price" not in p
        assert "selling_price" not in p


def test_pricing_absent_from_movement_export(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr27", "manager")
    dept_id = _create_department(app)
    part = _create_spare_part(client, name="ExportPriceCheck", buying_price="10", selling_price="20")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x", "department_id": dept_id,
    })
    res = client.get("/api/spare-parts/movements/export.csv")
    assert res.status_code == 200
    body = res.data.decode()
    assert "10" not in body.split("\n")[4]  # header row should not leak a bare buying price value
    assert "buying_price" not in body.lower()


# ---------- Opening Balance / Adjustments use selector (service-level contract) ----------

def test_opening_balance_requires_selected_spare_part(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr28", "manager")
    res = client.post("/api/spare-parts/movements/opening-balance", json={"spare_part_id": 999999, "quantity": "5"})
    assert res.status_code == 400


def test_adjustment_in_uses_selected_spare_part(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr29", "manager")
    part = _create_spare_part(client)
    res = client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "5", "direction": "in",
    })
    assert res.status_code == 201
    from webapp.services import spare_part_movement_service as movement_svc
    with app.app_context():
        assert movement_svc.current_stock(part["id"]) == 5


def test_no_direct_stock_editing_route_exists(client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr30", "manager")
    part = _create_spare_part(client)
    # Attempting to set current_stock_cache directly through the normal
    # PATCH route must be ignored — it's not a recognized field.
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"current_stock_cache": "9999"})
    assert res.status_code == 200
    assert res.get_json()["current_stock"] == "0"


# ---------- Regression: Dashboard / History / stock formula untouched ----------

def test_dashboard_still_works_after_refinement(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr31", "manager")
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    res = client.get("/api/spare-parts/dashboard")
    assert res.status_code == 200
    assert "total_active_spare_parts" in res.get_json()


def test_history_includes_department_and_machine_names(app, client, make_user):
    _login_spare_parts(client, make_user, "ref_mgr32", "manager")
    dept_id = _create_department(app, "HistoryDept")
    part = _create_spare_part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "5"})
    client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "x", "department_id": dept_id,
    })
    res = client.get("/api/spare-parts/movements")
    rows = res.get_json()
    stock_out_rows = [r for r in rows if r["movement_type"] == "STOCK_OUT"]
    assert stock_out_rows[0]["department_name"] == "HistoryDept"


def test_finished_goods_dashboard_unaffected(client, login_as):
    login_as("ref_fg_check", "password123", "manager")
    assert client.get("/api/dashboard").status_code == 200
