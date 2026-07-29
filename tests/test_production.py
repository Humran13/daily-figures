"""
Stage 5: the Production Book. Mirrors tests/test_returns.py's structure,
with the one real difference being that `shift` is mandatory here (Day or
Night) — Production is a genuinely shift-based workflow, unlike Returns.
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
    jumbomax = _make_product(client, "JumboMax Test", carton_to_pieces=24)
    return {"group_a": group_a, "jumbomax": jumbomax}


def _create_production(client, product_id, cartons=1, packs=0, pieces=0, date="2026-07-28", shift="Day", **kwargs):
    body = {"date": date, "shift": shift,
            "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}]}
    body.update(kwargs)
    return client.post("/api/production", json=body)


# ---------- creation ----------

def test_create_production_computes_base_units(client, setup):
    res = _create_production(client, setup["group_a"]["id"], cartons=2, packs=3, pieces=4)
    assert res.status_code == 201
    data = res.get_json()
    assert data["status"] == "draft"
    assert data["lines"][0]["base_unit_qty"] == 234


def test_create_production_no_pack_tier_product(client, setup):
    """JumboMax-style product (carton_to_pieces only) — same conversion
    rule Dispatch/Returns use, reused verbatim, never duplicated."""
    res = _create_production(client, setup["jumbomax"]["id"], cartons=3, pieces=2)
    assert res.status_code == 201
    assert res.get_json()["lines"][0]["base_unit_qty"] == 74  # 3*24 + 2


def test_create_production_multi_line(client, setup):
    body = {
        "date": "2026-07-28", "shift": "Day",
        "lines": [
            {"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
            {"product_id": setup["jumbomax"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
        ],
    }
    res = client.post("/api/production", json=body)
    assert res.status_code == 201
    assert len(res.get_json()["lines"]) == 2


def test_shift_is_mandatory(client, setup):
    res = client.post("/api/production", json={
        "date": "2026-07-28",
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


def test_shift_must_be_day_or_night(client, setup):
    res = _create_production(client, setup["group_a"]["id"], shift="Afternoon")
    assert res.status_code == 400


def test_night_production_recorded_correctly(client, setup):
    res = _create_production(client, setup["group_a"]["id"], shift="Night")
    assert res.status_code == 201
    assert res.get_json()["shift"] == "Night"


def test_create_production_requires_at_least_one_line(client, setup):
    res = client.post("/api/production", json={"date": "2026-07-28", "shift": "Day", "lines": []})
    assert res.status_code == 400


def test_production_line_zero_quantity_rejected(client, setup):
    res = _create_production(client, setup["group_a"]["id"], cartons=0, packs=0, pieces=0)
    assert res.status_code == 400


def test_production_product_without_packaging_rule_rejected(client, setup):
    unconfigured = client.post("/api/admin/products", json={"name": "Unconfigured"}).get_json()
    res = _create_production(client, unconfigured["id"])
    assert res.status_code == 400


def test_production_has_no_recipient_or_dispatch_number_fields(client, setup):
    """Simplicity requirement: Production has no customer/invoice concept at all."""
    res = _create_production(client, setup["group_a"]["id"])
    data = res.get_json()
    assert "customer_id" not in data
    assert "dispatch_number" not in data
    assert "invoice_number" not in data


# ---------- finalize / reopen / void ----------

def test_finalize_production(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/production/{created['id']}/finalize")
    assert res.status_code == 200
    assert res.get_json()["status"] == "finalized"


def test_reopen_finalized_production(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    res = client.post(f"/api/production/{created['id']}/reopen", json={"reason": "wrong quantity"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "draft"


def test_reopen_requires_reason(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    res = client.post(f"/api/production/{created['id']}/reopen", json={})
    assert res.status_code == 400


def test_void_production_requires_reason(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/production/{created['id']}/void", json={})
    assert res.status_code == 400


def test_void_production(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/production/{created['id']}/void", json={"reason": "duplicate entry"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "void"


# ---------- editing (draft only) ----------

def test_update_header_only_on_draft(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    res = client.patch(f"/api/production/{created['id']}", json={"remarks": "late correction"})
    assert res.status_code == 409


def test_update_header_can_change_shift_on_draft(client, setup):
    created = _create_production(client, setup["group_a"]["id"], shift="Day").get_json()
    res = client.patch(f"/api/production/{created['id']}", json={"shift": "Night"})
    assert res.status_code == 200
    assert res.get_json()["shift"] == "Night"


def test_add_line_to_draft_production(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    res = client.post(f"/api/production/{created['id']}/lines", json={
        "product_id": setup["jumbomax"]["id"], "cartons": 1, "pieces": 0,
    })
    assert res.status_code == 201
    assert len(res.get_json()["lines"]) == 2


def test_cannot_remove_last_line(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    line_id = created["lines"][0]["id"]
    res = client.delete(f"/api/production/{created['id']}/lines/{line_id}")
    assert res.status_code == 400


def test_cannot_edit_lines_on_finalized_production(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    line_id = created["lines"][0]["id"]
    res = client.patch(f"/api/production/{created['id']}/lines/{line_id}", json={"cartons": 5})
    assert res.status_code == 409


# ---------- permissions ----------

def test_operator_can_create_and_finalize_own_draft(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = _create_production(client, setup["group_a"]["id"])
    assert res.status_code == 201
    created = res.get_json()
    fin = client.post(f"/api/production/{created['id']}/finalize")
    assert fin.status_code == 200


def test_viewer_cannot_create_production(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = _create_production(client, setup["group_a"]["id"])
    assert res.status_code == 403


def test_viewer_can_list_and_export_production(client, setup, login_as):
    _create_production(client, setup["group_a"]["id"])
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    assert client.get("/api/production").status_code == 200
    assert client.get("/api/production/export.csv").status_code == 200


def test_viewer_cannot_finalize_or_void(client, setup, login_as):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    assert client.post(f"/api/production/{created['id']}/finalize").status_code == 403
    assert client.post(f"/api/production/{created['id']}/void", json={"reason": "x"}).status_code == 403


def test_operator_cannot_edit_others_draft(client, setup, login_as):
    created = _create_production(client, setup["group_a"]["id"]).get_json()  # created by root
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.patch(f"/api/production/{created['id']}", json={"remarks": "trying to edit"})
    assert res.status_code == 403


def test_operator_cannot_void_or_reopen(client, setup, login_as):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    assert client.post(f"/api/production/{created['id']}/void", json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/production/{created['id']}/reopen", json={"reason": "x"}).status_code == 403


def test_manager_can_void_and_reopen(client, setup, login_as):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    assert client.post(f"/api/production/{created['id']}/reopen", json={"reason": "correction"}).status_code == 200
    client.post(f"/api/production/{created['id']}/finalize")
    assert client.post(f"/api/production/{created['id']}/void", json={"reason": "done"}).status_code == 200


def test_direct_api_request_unauthenticated_is_rejected(client, setup):
    client.post("/api/logout")
    res = _create_production(client, setup["group_a"]["id"])
    assert res.status_code == 401


# ---------- audit ----------

def test_create_and_finalize_production_are_audited(client, setup, app):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="create", entity_type="production").first() is not None
        assert AuditLog.query.filter_by(action="finalize", entity_type="production").first() is not None


def test_void_production_is_audited(client, setup, app):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/void", json={"reason": "test"})
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="void", entity_type="production").first() is not None


# ---------- filtering / no duplicate rows ----------

def test_product_filter_does_not_duplicate_production_with_multiple_lines_of_same_product(client, setup):
    created = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{created['id']}/lines", json={
        "product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 1,
    })

    res = client.get(f"/api/production?product_id={setup['group_a']['id']}")
    data = res.get_json()
    assert data["total"] == 1
    assert len(data["results"]) == 1


def test_shift_filter(client, setup):
    day = _create_production(client, setup["group_a"]["id"], shift="Day").get_json()
    night = _create_production(client, setup["group_a"]["id"], shift="Night").get_json()

    res = client.get("/api/production?shift=Night")
    ids = {r["id"] for r in res.get_json()["results"]}
    assert ids == {night["id"]}
    assert day["id"] not in ids


def test_status_filter(client, setup):
    draft = _create_production(client, setup["group_a"]["id"]).get_json()
    finalized = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{finalized['id']}/finalize")

    res = client.get("/api/production?status=finalized")
    ids = {r["id"] for r in res.get_json()["results"]}
    assert finalized["id"] in ids
    assert draft["id"] not in ids


# ---------- exports ----------

def test_export_csv_uses_business_friendly_quantity_not_raw_pieces(client, setup):
    created = _create_production(client, setup["group_a"]["id"], cartons=2, packs=3, pieces=4).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    res = client.get("/api/production/export.csv")
    assert res.status_code == 200
    assert b"2.34 Ctns" in res.data
    assert b"234" not in res.data


def test_export_xlsx_and_pdf_work(client, setup):
    created = _create_production(client, setup["group_a"]["id"]).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    xlsx = client.get("/api/production/export.xlsx")
    assert xlsx.status_code == 200
    pdf = client.get("/api/production/export.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_export_is_audited(client, setup, app):
    client.get("/api/production/export.csv")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="export", entity_type="production").first() is not None
