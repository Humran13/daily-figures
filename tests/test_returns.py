"""
Stage 5: the Returns Book. Mirrors tests/test_dispatches.py's structure —
creation, multi-line entries, draft/finalized/void lifecycle, permissions,
audit, exports, and EXISTS-subquery product filtering (never a join, so a
return with several lines for one product never multiplies the parent row
— see webapp/routes/returns.py's _filtered_return_query).
"""
import pytest


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10, carton_to_pieces=None):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    body = {"carton_to_pieces": carton_to_pieces} if carton_to_pieces else \
        {"cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces}
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json=body)
    return product


@pytest.fixture
def setup(client, super_admin):
    group_a = _make_product(client, "Compact Corporate Test")
    kingmax = _make_product(client, "KingMax Test", carton_to_pieces=60)
    category = client.post("/api/admin/sales-categories", json={"name": "Test Sales Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"group_a": group_a, "kingmax": kingmax, "customer": customer, "category": category}


def _create_return(client, product_id, cartons=1, packs=0, pieces=0, date="2026-07-28", **kwargs):
    body = {"date": date, "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}]}
    body.update(kwargs)
    return client.post("/api/returns", json=body)


# ---------- creation ----------

def test_create_return_computes_base_units(client, setup):
    res = _create_return(client, setup["group_a"]["id"], cartons=2, packs=3, pieces=4)
    assert res.status_code == 201
    data = res.get_json()
    assert data["status"] == "draft"
    assert data["lines"][0]["base_unit_qty"] == 234


def test_create_return_no_pack_tier_product(client, setup):
    """KingMax-style product (carton_to_pieces only, no pack unit) must not
    get a packs field/interpretation — same conversion rule Dispatch uses,
    reused verbatim, never duplicated."""
    res = _create_return(client, setup["kingmax"]["id"], cartons=2, pieces=5)
    assert res.status_code == 201
    assert res.get_json()["lines"][0]["base_unit_qty"] == 125  # 2*60 + 5


def test_create_return_multi_line(client, setup):
    body = {
        "date": "2026-07-28",
        "lines": [
            {"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
            {"product_id": setup["kingmax"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
        ],
    }
    res = client.post("/api/returns", json=body)
    assert res.status_code == 201
    assert len(res.get_json()["lines"]) == 2


def test_create_return_requires_at_least_one_line(client, setup):
    res = client.post("/api/returns", json={"date": "2026-07-28", "lines": []})
    assert res.status_code == 400


def test_create_return_reuses_existing_customer_as_returned_by(client, setup):
    res = _create_return(client, setup["group_a"]["id"], customer_id=setup["customer"]["id"])
    assert res.status_code == 201
    data = res.get_json()
    assert data["returned_by_customer_id"] == setup["customer"]["id"]
    assert data["returned_by_name"] == "Dalca"


def test_create_return_accepts_free_text_returned_by(client, setup):
    res = _create_return(client, setup["group_a"]["id"], returned_by_name="Truck 7 / Field Rep Musa")
    assert res.status_code == 201
    data = res.get_json()
    assert data["returned_by_customer_id"] is None
    assert data["returned_by_name"] == "Truck 7 / Field Rep Musa"


def test_return_defaults_received_by_to_creating_user(client, setup, app):
    res = _create_return(client, setup["group_a"]["id"])
    data = res.get_json()
    assert data["received_by_username"] == "root"
    assert data["verified_by_username"] is None  # not verified until finalized


def test_signed_by_name_recorded_as_digital_signature_stand_in(client, setup):
    res = _create_return(client, setup["group_a"]["id"], signed_by_name="J. Otieno")
    assert res.get_json()["signed_by_name"] == "J. Otieno"


def test_return_line_zero_quantity_rejected(client, setup):
    res = _create_return(client, setup["group_a"]["id"], cartons=0, packs=0, pieces=0)
    assert res.status_code == 400


def test_return_product_without_packaging_rule_rejected(client, setup):
    unconfigured = client.post("/api/admin/products", json={"name": "Unconfigured"}).get_json()
    res = _create_return(client, unconfigured["id"])
    assert res.status_code == 400


# ---------- finalize / reopen / void ----------

def test_finalize_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/returns/{created['id']}/finalize")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "finalized"
    assert data["finalized_by"] is not None
    assert data["verified_by_username"] == "root"  # finalizing is the verification step


def test_finalize_return_requires_lines(client, setup, app):
    # can't easily create a zero-line return via the API (create_return
    # requires at least one) — this confirms the service-level guard exists
    # for defense in depth, mirroring dispatch_service's identical check.
    from webapp.models.return_record import ReturnRecord
    from webapp.models.user import User
    from webapp.services.returns_service import ReturnError, finalize_return
    with app.app_context():
        user = User.query.filter_by(username="root").first()
        with pytest.raises(ReturnError):
            finalize_return(ReturnRecord(date="2026-07-28"), user)


def test_reopen_finalized_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.post(f"/api/returns/{created['id']}/reopen", json={"reason": "wrong quantity"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "draft"


def test_reopen_requires_reason(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.post(f"/api/returns/{created['id']}/reopen", json={})
    assert res.status_code == 400


def test_void_return_requires_reason(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/returns/{created['id']}/void", json={})
    assert res.status_code == 400


def test_void_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/returns/{created['id']}/void", json={"reason": "duplicate entry"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# ---------- editing (draft only) ----------

def test_update_header_only_on_draft(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "late correction"})
    assert res.status_code == 409


def test_add_line_to_draft_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/returns/{created['id']}/lines", json={
        "product_id": setup["kingmax"]["id"], "cartons": 1, "pieces": 0,
    })
    assert res.status_code == 201
    assert len(res.get_json()["lines"]) == 2


def test_cannot_remove_last_line(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    line_id = created["lines"][0]["id"]
    res = client.delete(f"/api/returns/{created['id']}/lines/{line_id}")
    assert res.status_code == 400


def test_update_line_on_draft_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"], cartons=1).get_json()
    line_id = created["lines"][0]["id"]
    res = client.patch(f"/api/returns/{created['id']}/lines/{line_id}", json={"cartons": 3})
    assert res.status_code == 200
    assert res.get_json()["lines"][0]["cartons"] == 3


def test_cannot_edit_lines_on_finalized_return(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    line_id = created["lines"][0]["id"]
    res = client.patch(f"/api/returns/{created['id']}/lines/{line_id}", json={"cartons": 5})
    assert res.status_code == 409


# ---------- permissions ----------

def test_operator_can_create_and_finalize_own_draft(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = _create_return(client, setup["group_a"]["id"])
    assert res.status_code == 201
    created = res.get_json()
    fin = client.post(f"/api/returns/{created['id']}/finalize")
    assert fin.status_code == 200


def test_viewer_cannot_create_return(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = _create_return(client, setup["group_a"]["id"])
    assert res.status_code == 403


def test_viewer_can_list_and_export_returns(client, setup, login_as):
    _create_return(client, setup["group_a"]["id"])
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    assert client.get("/api/returns").status_code == 200
    assert client.get("/api/returns/export.csv").status_code == 200


def test_viewer_cannot_finalize_or_void(client, setup, login_as):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    assert client.post(f"/api/returns/{created['id']}/finalize").status_code == 403
    assert client.post(f"/api/returns/{created['id']}/void", json={"reason": "x"}).status_code == 403


def test_operator_cannot_edit_others_draft(client, setup, login_as):
    created = _create_return(client, setup["group_a"]["id"]).get_json()  # created by root
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "trying to edit"})
    assert res.status_code == 403


def test_operator_cannot_void_or_reopen(client, setup, login_as):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    assert client.post(f"/api/returns/{created['id']}/void", json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/returns/{created['id']}/reopen", json={"reason": "x"}).status_code == 403


def test_manager_can_void_and_reopen(client, setup, login_as):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    assert client.post(f"/api/returns/{created['id']}/reopen", json={"reason": "correction"}).status_code == 200
    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.post(f"/api/returns/{created['id']}/void", json={"reason": "done"}).status_code == 200


def test_direct_api_request_unauthenticated_is_rejected(client, setup):
    client.post("/api/logout")
    res = _create_return(client, setup["group_a"]["id"])
    assert res.status_code == 401


# ---------- audit ----------

def test_create_and_finalize_return_are_audited(client, setup, app):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="create", entity_type="return").first() is not None
        assert AuditLog.query.filter_by(action="finalize", entity_type="return").first() is not None


def test_void_return_is_audited(client, setup, app):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/void", json={"reason": "test"})
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="void", entity_type="return").first()
        assert entry is not None


# ---------- filtering / no duplicate rows ----------

def test_product_filter_does_not_duplicate_return_with_multiple_lines_of_same_product(client, setup):
    """The EXISTS-subquery regression guard — a join(ReturnLine) would
    multiply this return once per matching line."""
    body = {
        "date": "2026-07-28",
        "lines": [
            {"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0, "line_notes": "first"},
        ],
    }
    created = client.post("/api/returns", json=body).get_json()
    # add a SECOND line for the SAME product
    client.post(f"/api/returns/{created['id']}/lines", json={
        "product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 1,
    })

    res = client.get(f"/api/returns?product_id={setup['group_a']['id']}")
    data = res.get_json()
    assert data["total"] == 1
    assert len(data["results"]) == 1


def test_status_filter(client, setup):
    draft = _create_return(client, setup["group_a"]["id"]).get_json()
    finalized = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{finalized['id']}/finalize")

    res = client.get("/api/returns?status=finalized")
    ids = {r["id"] for r in res.get_json()["results"]}
    assert finalized["id"] in ids
    assert draft["id"] not in ids


def test_date_range_filter(client, setup):
    _create_return(client, setup["group_a"]["id"], date="2026-07-01")
    in_range = _create_return(client, setup["group_a"]["id"], date="2026-07-28").get_json()

    res = client.get("/api/returns?date_from=2026-07-20&date_to=2026-07-31")
    ids = {r["id"] for r in res.get_json()["results"]}
    assert ids == {in_range["id"]}


# ---------- exports ----------

def test_export_csv_uses_business_friendly_quantity_not_raw_pieces(client, setup):
    created = _create_return(client, setup["group_a"]["id"], cartons=2, packs=3, pieces=4).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.get("/api/returns/export.csv")
    assert res.status_code == 200
    assert b"2c 3p 4pc" in res.data
    assert b"234" not in res.data  # raw base-unit total must not appear as the primary quantity


def test_export_xlsx_and_pdf_work(client, setup):
    created = _create_return(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    xlsx = client.get("/api/returns/export.xlsx")
    assert xlsx.status_code == 200
    pdf = client.get("/api/returns/export.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_export_is_audited(client, setup, app):
    client.get("/api/returns/export.csv")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="export", entity_type="return").first() is not None
