"""
Spare Parts management hardening: shared search, Super-Admin-only permanent
delete (with cascade, atomic rollback and audit), Edit/Activate/Delete on the
supporting masters, and the Admin > Spare Parts > Pricing & Profit API.
"""
import json
from decimal import Decimal

from sqlalchemy.exc import OperationalError

from webapp.extensions import db
from webapp.models.audit_log import AuditLog
from webapp.models.machine import Machine
from webapp.models.operational_department import SparePartDepartment
from webapp.models.spare_part import SparePart, SparePartMachine, SparePartSupplier
from webapp.models.spare_part_import_row import SparePartImportRow
from webapp.models.spare_part_movement import SparePartMovement
from webapp.models.spare_part_category import SparePartCategory
from webapp.models.supplier import Supplier
from webapp.services import spare_part_service as svc


def _login(client, make_user, username, role, password="password123"):
    from tests.test_spare_parts import _login_spare_parts
    return _login_spare_parts(client, make_user, username, role, password)


def _post(client, url, body):
    res = client.post(url, json=body)
    return res


def _spare(client, name, specifications=None, **extra):
    body = {"name": name, "specifications": specifications, "unit": "pcs"}
    body.update(extra)
    res = client.post("/api/spare-parts", json=body)
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def _ids(rows):
    return {r["id"] for r in rows}


# ---------- shared search ----------

def _search_fixture(client, make_user):
    _login(client, make_user, "search_mgr", "manager")
    bearing_22213 = _spare(client, "Bearing", "22213")
    bearing_6304 = _spare(client, "Bearing", "6304")
    belt = _spare(client, "V-Belt", "A42")
    res = client.post("/api/spare-parts/machines", json={"name": "Press 1"})
    machine_id = res.get_json()["id"]
    client.patch(f"/api/spare-parts/{belt['id']}", json={"machine_ids": [machine_id]})
    return bearing_22213, bearing_6304, belt


def _search(client, q, **params):
    qs = {"q": q, **params}
    res = client.get("/api/spare-parts", query_string=qs)
    assert res.status_code == 200
    return res.get_json()


def test_name_and_specification_both_find_the_same_bearing(client, make_user):
    b22213, _, _ = _search_fixture(client, make_user)
    assert b22213["id"] in _ids(_search(client, "Bearing"))
    assert _ids(_search(client, "22213")) == {b22213["id"]}


def test_partial_name_and_partial_specification_match(client, make_user):
    b22213, _, _ = _search_fixture(client, make_user)
    assert _ids(_search(client, "Bear")) >= {b22213["id"]}
    assert _ids(_search(client, "2221")) == {b22213["id"]}


def test_search_is_case_insensitive(client, make_user):
    b22213, b6304, _ = _search_fixture(client, make_user)
    assert _ids(_search(client, "BEARING")) == {b22213["id"], b6304["id"]}
    assert _ids(_search(client, "v-belt")) and _ids(_search(client, "V-BELT"))


def test_multiple_tokens_must_all_match(client, make_user):
    b22213, _, _ = _search_fixture(client, make_user)
    assert _ids(_search(client, "bearing 22213")) == {b22213["id"]}
    assert _search(client, "bearing 99999") == []


def test_search_matches_linked_machine_name(client, make_user):
    _, _, belt = _search_fixture(client, make_user)
    assert _ids(_search(client, "Press")) == {belt["id"]}


def test_search_treats_wildcards_literally(client, make_user):
    _search_fixture(client, make_user)
    assert _search(client, "%") == []
    assert _search(client, "_") == []


def test_inactive_parts_hidden_by_default_and_shown_when_requested(client, make_user):
    b22213, _, _ = _search_fixture(client, make_user)
    client.patch(f"/api/spare-parts/{b22213['id']}", json={"active": False})
    assert b22213["id"] not in _ids(_search(client, "22213"))
    assert b22213["id"] in _ids(_search(client, "22213", include_inactive="1"))


def test_search_service_function_matches_endpoint(app, client, make_user):
    b22213, _, _ = _search_fixture(client, make_user)
    with app.app_context():
        assert {p.id for p in svc.search_spare_parts("22213")} == {b22213["id"]}


# ---------- permanent delete (Super Administrator) ----------

def _seed_related(app, client, make_user):
    """A spare part with movements (in, out, adjustment), a machine link, a
    supplier quote and an import provenance row — everything a delete must
    cascade or detach."""
    _login(client, make_user, "related_mgr", "manager")
    dept = client.post("/api/spare-parts/departments", json={"name": "Weaving"}).get_json()
    machine = client.post("/api/spare-parts/machines", json={"name": "Loom 7"}).get_json()
    supplier = client.post("/api/spare-parts/suppliers", json={"name": "Acme Bearings"}).get_json()
    part = _spare(client, "Bearing", "22213", machine_ids=[machine["id"]])

    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "2", "requested_by": "Asha",
        "department_id": dept["id"], "machine_id": machine["id"],
    })
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "1", "direction": "in", "reason": "count",
    })
    with app.app_context():
        db.session.add(SparePartSupplier(spare_part_id=part["id"], supplier_id=supplier["id"], raw_price_text="45k"))
        db.session.add(SparePartImportRow(
            source_file="seed.xlsx", sheet_name="Sheet1", source_row=12, raw_json="{}",
            spare_part_id=part["id"], import_batch_id="batch-1",
        ))
        db.session.commit()
    return part, machine, supplier, dept


def test_manager_cannot_permanently_delete(app, client, make_user):
    part, *_ = _seed_related(app, client, make_user)
    res = client.delete(f"/api/spare-parts/{part['id']}")
    assert res.status_code == 403
    with app.app_context():
        assert db.session.get(SparePart, part["id"]) is not None


def test_deletion_preview_reports_related_counts(app, client, make_user):
    part, *_ = _seed_related(app, client, make_user)
    _login(client, make_user, "preview_admin", "super_admin")
    body = client.get(f"/api/spare-parts/{part['id']}/deletion-preview").get_json()
    assert body["related"] == {"movements": 3, "machine_links": 1, "supplier_quotes": 1, "import_rows_detached": 1}


def test_super_admin_permanently_deletes_with_history_and_audits(app, client, make_user):
    part, machine, supplier, dept = _seed_related(app, client, make_user)
    _login(client, make_user, "delete_admin", "super_admin")

    res = client.delete(f"/api/spare-parts/{part['id']}")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["removed"]["movements"] == 3

    with app.app_context():
        assert db.session.get(SparePart, part["id"]) is None
        assert SparePartMovement.query.filter_by(spare_part_id=part["id"]).count() == 0
        assert SparePartMachine.query.filter_by(spare_part_id=part["id"]).count() == 0
        assert SparePartSupplier.query.filter_by(spare_part_id=part["id"]).count() == 0
        # Import provenance is kept but no longer points at a deleted part.
        row = SparePartImportRow.query.filter_by(import_batch_id="batch-1").one()
        assert row.spare_part_id is None
        # The shared masters themselves are untouched.
        assert db.session.get(Machine, machine["id"]) is not None
        assert db.session.get(Supplier, supplier["id"]) is not None

        entry = AuditLog.query.filter_by(action="permanent_delete", entity_type="spare_part",
                                         entity_id=str(part["id"])).one()
        assert entry.username == "delete_admin"
        after = json.loads(entry.after_json)
        assert after["removed_related"] == {"movements": 3, "machine_links": 1,
                                            "supplier_quotes": 1, "import_rows_detached": 1}
        assert json.loads(entry.before_json)["name"] == "Bearing"


def test_permanent_delete_is_atomic_on_failure(app, client, make_user, monkeypatch):
    part, *_ = _seed_related(app, client, make_user)
    _login(client, make_user, "atomic_admin", "super_admin")

    def fail_midway(spare_part, actor):
        SparePartMovement.query.filter_by(spare_part_id=spare_part.id).delete(synchronize_session=False)
        raise OperationalError("simulated failure", None, Exception("boom"))

    monkeypatch.setattr(svc, "permanently_delete_spare_part", fail_midway)
    res = client.delete(f"/api/spare-parts/{part['id']}")
    assert res.status_code == 500
    assert "Nothing was removed" in res.get_json()["error"]

    with app.app_context():
        assert db.session.get(SparePart, part["id"]) is not None
        assert SparePartMovement.query.filter_by(spare_part_id=part["id"]).count() == 3


def test_delete_missing_spare_part_returns_404(client, make_user):
    _login(client, make_user, "missing_admin", "super_admin")
    assert client.delete("/api/spare-parts/999999").status_code == 404


# ---------- supporting masters: edit / activate / delete ----------

def _masters(client):
    return {
        "category": ("/api/spare-parts/categories", {"name": "Lubricants"}),
        "department": ("/api/spare-parts/departments", {"name": "Packing"}),
        "machine": ("/api/spare-parts/machines", {"name": "Filler 3"}),
        "supplier": ("/api/spare-parts/suppliers", {"name": "Nile Parts", "building": "B1"}),
    }


def test_masters_add_edit_deactivate_reactivate(client, make_user):
    _login(client, make_user, "masters_mgr", "manager")
    for kind, (base, body) in _masters(client).items():
        created = client.post(base, json=body)
        assert created.status_code == 201, (kind, created.get_json())
        item_id = created.get_json()["id"]

        renamed = client.patch(f"{base}/{item_id}", json={"name": body["name"] + " X"})
        assert renamed.status_code == 200, (kind, renamed.get_json())
        assert renamed.get_json()["name"] == body["name"] + " X"

        off = client.patch(f"{base}/{item_id}", json={"active": False})
        assert off.get_json()["active"] is False
        on = client.patch(f"{base}/{item_id}", json={"active": True})
        assert on.get_json()["active"] is True


def test_masters_delete_is_super_admin_only(client, make_user):
    _login(client, make_user, "masters_mgr2", "manager")
    for kind, (base, body) in _masters(client).items():
        item_id = client.post(base, json=body).get_json()["id"]
        assert client.delete(f"{base}/{item_id}").status_code == 403, kind


def test_masters_permanent_delete_by_super_admin_detaches_without_losing_stock(app, client, make_user):
    part, machine, supplier, dept = _seed_related(app, client, make_user)
    _login(client, make_user, "masters_admin", "super_admin")

    assert client.delete(f"/api/spare-parts/departments/{dept['id']}").status_code == 200
    assert client.delete(f"/api/spare-parts/machines/{machine['id']}").status_code == 200
    assert client.delete(f"/api/spare-parts/suppliers/{supplier['id']}").status_code == 200

    with app.app_context():
        movements = SparePartMovement.query.filter_by(spare_part_id=part["id"]).all()
        assert len(movements) == 3  # stock history survives the master deletions
        assert all(m.department_id is None and m.machine_id is None and m.supplier_id is None
                   for m in movements)
        assert db.session.get(SparePartDepartment, dept["id"]) is None
        assert db.session.get(Machine, machine["id"]) is None
        assert db.session.get(Supplier, supplier["id"]) is None
        assert SparePartSupplier.query.filter_by(supplier_id=supplier["id"]).count() == 0
        for action, entity in (("permanent_delete", "spare_part_department"),
                               ("permanent_delete", "machine"),
                               ("permanent_delete", "spare_supplier")):
            assert AuditLog.query.filter_by(action=action, entity_type=entity).count() >= 1

    # The part still reports its stock after its links were detached.
    assert Decimal(client.get(f"/api/spare-parts/{part['id']}").get_json()["current_stock"]) == Decimal("9")


def test_category_delete_uncategorizes_spare_parts(app, client, make_user):
    _login(client, make_user, "cat_admin", "super_admin")
    category = client.post("/api/spare-parts/categories", json={"name": "Electrical"}).get_json()
    part = _spare(client, "Relay", "24V", category_id=category["id"])
    assert client.delete(f"/api/spare-parts/categories/{category['id']}").status_code == 200
    with app.app_context():
        assert db.session.get(SparePartCategory, category["id"]) is None
        assert db.session.get(SparePart, part["id"]).category_id is None


def test_master_delete_missing_returns_404(client, make_user):
    _login(client, make_user, "missing_master_admin", "super_admin")
    assert client.delete("/api/spare-parts/machines/999999").status_code == 404
    assert client.delete("/api/spare-parts/suppliers/999999").status_code == 404
    assert client.delete("/api/spare-parts/departments/999999").status_code == 404
    assert client.delete("/api/spare-parts/categories/999999").status_code == 404


# ---------- Admin > Spare Parts > Pricing & Profit ----------

def test_pricing_api_is_super_admin_only(client, make_user):
    _login(client, make_user, "price_viewer_mgr", "manager")
    assert client.get("/api/admin/spare-parts/pricing").status_code == 403
    _login(client, make_user, "price_op", "operator")
    assert client.get("/api/admin/spare-parts/pricing").status_code == 403


def test_manager_cannot_set_prices_on_master_data(client, make_user):
    _login(client, make_user, "price_setter_mgr", "manager")
    res = client.post("/api/spare-parts", json={"name": "Priced By Manager", "buying_price": "10"})
    assert res.status_code == 403
    part = _spare(client, "Plain Part")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"selling_price": "20"})
    assert res.status_code == 403


def test_profit_margin_and_valuation_match_example(client, make_user):
    _login(client, make_user, "price_super", "super_admin")
    part = _spare(client, "Gear", "M5", buying_price="40000", selling_price="55000")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})

    body = client.get("/api/admin/spare-parts/pricing").get_json()
    row = next(r for r in body["rows"] if r["id"] == part["id"])
    assert Decimal(row["profit_per_unit"]) == Decimal("15000.00")
    assert Decimal(row["margin_percent"]).quantize(Decimal("0.01")) == Decimal("27.27")
    assert Decimal(row["current_stock"]) == Decimal("10.000")
    assert Decimal(row["stock_value_at_buying"]) == Decimal("400000.00")
    assert Decimal(row["expected_sales_value"]) == Decimal("550000.00")
    assert Decimal(row["expected_gross_profit"]) == Decimal("150000.00")
    assert Decimal(body["totals"]["expected_gross_profit"]) >= Decimal("150000.00")


def test_pricing_figures_are_null_when_price_missing(client, make_user):
    _login(client, make_user, "price_null_super", "super_admin")
    part = _spare(client, "Unpriced Thing")
    row = next(r for r in client.get("/api/admin/spare-parts/pricing").get_json()["rows"] if r["id"] == part["id"])
    assert row["profit_per_unit"] is None
    assert row["margin_percent"] is None
    assert row["expected_gross_profit"] is None


def test_pricing_search_uses_shared_matching(client, make_user):
    _login(client, make_user, "price_search_super", "super_admin")
    part = _spare(client, "Bearing", "22213", buying_price="1000", selling_price="1500")
    _spare(client, "Bearing", "6304", buying_price="900", selling_price="1200")
    rows = client.get("/api/admin/spare-parts/pricing", query_string={"q": "22213"}).get_json()["rows"]
    assert [r["id"] for r in rows] == [part["id"]]


def test_price_update_is_recorded_in_history(client, make_user):
    _login(client, make_user, "price_hist_super", "super_admin")
    part = _spare(client, "Hose", "1in", buying_price="100", selling_price="150")

    res = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}",
                       json={"buying_price": "120", "selling_price": "160"})
    assert res.status_code == 200
    assert Decimal(res.get_json()["profit_per_unit"]) == Decimal("40.00")

    # Unchanged save writes no history row.
    client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={"buying_price": "120", "selling_price": "160"})

    history = client.get(f"/api/admin/spare-parts/pricing/{part['id']}/history").get_json()
    assert len(history) == 1
    entry = history[0]
    assert entry["username"] == "price_hist_super"
    assert {k: Decimal(v) for k, v in entry["before"].items()} == {"buying_price": Decimal("100"), "selling_price": Decimal("150")}
    assert {k: Decimal(v) for k, v in entry["after"].items()} == {"buying_price": Decimal("120"), "selling_price": Decimal("160")}
    assert entry["created_at"]


def test_price_change_via_master_patch_is_also_recorded(client, make_user):
    _login(client, make_user, "price_master_super", "super_admin")
    part = _spare(client, "Seal", "O-ring", buying_price="5", selling_price="8")
    client.patch(f"/api/spare-parts/{part['id']}", json={"selling_price": "9"})
    history = client.get(f"/api/admin/spare-parts/pricing/{part['id']}/history").get_json()
    assert Decimal(history[0]["before"]["selling_price"]) == Decimal("8")
    assert Decimal(history[0]["after"]["selling_price"]) == Decimal("9")


def test_negative_price_rejected(client, make_user):
    _login(client, make_user, "price_neg_super", "super_admin")
    part = _spare(client, "Pump", "2in")
    res = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={"buying_price": "-5"})
    assert res.status_code == 400


def test_pricing_routes_reject_unauthenticated(client):
    assert client.get("/api/admin/spare-parts/pricing").status_code == 401
    assert client.get("/api/admin/spare-parts/pricing/1/history").status_code == 401


# ---------- page-level checks (layout contract + access to the Admin pricing page) ----------

def test_master_data_table_is_contained_with_required_columns(client, make_user):
    _login(client, make_user, "page_mgr", "manager")
    html = client.get("/spare-parts-master.html").get_data(as_text=True)
    wrap_start = html.index('<div class="table-wrap">')
    table_start = html.index('id="sparePartsTable"')
    wrap_end = html.index("</table>", table_start) + len("</table>")
    assert wrap_start < table_start < wrap_end
    header = html[html.index("<thead><tr><th>Name</th><th>Stock</th><th>Price</th><th>Status</th><th>Actions</th>"):]
    assert header.startswith("<thead><tr><th>Name</th><th>Stock</th><th>Price</th><th>Status</th><th>Actions</th>")
    assert 'data-action="delete"' in html and "isSuper" in html  # Delete only rendered for Super Admin
    assert "/spare-parts-search.js" in html


def test_spare_parts_pickers_load_shared_search(client, make_user):
    _login(client, make_user, "page_op", "operator")
    for page in ("/spare-parts-stock-in.html", "/spare-parts-stock-out.html"):
        html = client.get(page).get_data(as_text=True)
        assert "SparePartSearch.source()" in html, page
        assert "/spare-parts-search.js" in html, page


def test_admin_page_is_super_admin_only_and_has_pricing_tab(client, make_user):
    _login(client, make_user, "page_admin_mgr", "manager")
    assert client.get("/admin.html").status_code == 302

    _login(client, make_user, "page_super", "super_admin")
    res = client.get("/admin.html")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'data-panel="sp-pricing"' in html and 'id="panel-sp-pricing"' in html
