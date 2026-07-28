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


def _make_customer(client, category_id, name="Dalca"):
    return client.post("/api/admin/customers", json={"name": name, "sales_category_id": category_id}).get_json()


@pytest.fixture
def setup(client, super_admin):
    """A super_admin session plus one Group-A product, one no-pack-tier product, and a customer.

    The customer is given a sales category so every dispatch created here
    satisfies the "every new dispatch has a category" requirement by
    derivation, without every individual test needing to pass one explicitly.
    """
    group_a = _make_product(client, "Compact Corporate Test")
    kingmax = _make_product(client, "KingMax Test", carton_to_pieces=60)
    category = client.post("/api/admin/sales-categories", json={"name": "Test Sales Category"}).get_json()
    customer = _make_customer(client, category["id"])
    return {"group_a": group_a, "kingmax": kingmax, "customer": customer, "category": category}


def test_create_dispatch_computes_base_units(client, setup):
    body = {
        "dispatch_number": "D-1001",
        "date": "2026-07-28",
        "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [
            {"product_id": setup["group_a"]["id"], "cartons": 2, "packs": 3, "pieces": 4},
        ],
    }
    res = client.post("/api/dispatches", json=body)
    assert res.status_code == 201
    data = res.get_json()
    assert data["status"] == "draft"
    assert len(data["lines"]) == 1
    assert data["lines"][0]["base_unit_qty"] == 234  # spec's worked example


def test_create_dispatch_normalizes_excess_quantities(client, setup):
    body = {
        "dispatch_number": "D-1002",
        "date": "2026-07-28",
        "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 0, "packs": 13, "pieces": 24}],
    }
    res = client.post("/api/dispatches", json=body)
    line = res.get_json()["lines"][0]
    assert (line["cartons"], line["packs"], line["pieces"]) == (1, 5, 4)


def test_no_pack_tier_product_rejects_nonzero_packs(client, setup):
    body = {
        "dispatch_number": "D-1003",
        "date": "2026-07-28",
        "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["kingmax"]["id"], "cartons": 1, "packs": 1, "pieces": 0}],
    }
    res = client.post("/api/dispatches", json=body)
    assert res.status_code == 400


def test_product_without_packaging_rule_rejected(client, setup):
    unconfigured = client.post("/api/admin/products", json={"name": "Unconfigured Thing"}).get_json()
    body = {
        "dispatch_number": "D-1004",
        "date": "2026-07-28",
        "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": unconfigured["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }
    res = client.post("/api/dispatches", json=body)
    assert res.status_code == 400
    assert "no packaging rule" in res.get_json()["error"]


def test_empty_lines_rejected(client, setup):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "D-1005", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"], "lines": [],
    })
    assert res.status_code == 400


def test_duplicate_dispatch_number_rejected_for_non_super_admin(client, setup, login_as):
    client.post("/api/dispatches", json={
        "dispatch_number": "DUP-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "DUP-1", "date": "2026-07-29", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 409


def test_duplicate_dispatch_number_override_by_super_admin(client, setup, app):
    client.post("/api/dispatches", json={
        "dispatch_number": "DUP-2", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.post("/api/dispatches", json={
        "dispatch_number": "DUP-2", "date": "2026-07-29", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
        "override_duplicate": True, "override_reason": "Reprinted physical note, same number intentional",
    })
    assert res.status_code == 201
    assert res.get_json()["duplicate_override"] is True

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="duplicate_number_override").first()
        assert entry is not None


def test_duplicate_override_without_reason_rejected(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "DUP-3", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.post("/api/dispatches", json={
        "dispatch_number": "DUP-3", "date": "2026-07-29", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
        "override_duplicate": True,
    })
    assert res.status_code == 400


def test_viewer_cannot_create_dispatch(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "V-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_operator_cannot_edit_another_operators_draft(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    created = client.post("/api/dispatches", json={
        "dispatch_number": "O-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post("/api/logout")
    login_as("op2", "password123", "operator")
    res = client.patch(f"/api/dispatches/{created['id']}", json={"notes": "sneaky edit"})
    assert res.status_code == 403


def test_manager_can_edit_any_operators_draft(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    created = client.post("/api/dispatches", json={
        "dispatch_number": "O-2", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    res = client.patch(f"/api/dispatches/{created['id']}", json={"notes": "manager correction"})
    assert res.status_code == 200


def test_finalize_requires_lines_and_locks_editing(client, setup):
    created = client.post("/api/dispatches", json={
        "dispatch_number": "F-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    fin = client.post(f"/api/dispatches/{created['id']}/finalize")
    assert fin.status_code == 200
    assert fin.get_json()["status"] == "finalized"
    assert fin.get_json()["finalized_at"] is not None

    edit = client.patch(f"/api/dispatches/{created['id']}", json={"notes": "too late"})
    assert edit.status_code == 409

    add_line = client.post(f"/api/dispatches/{created['id']}/lines", json={
        "product_id": setup["kingmax"]["id"], "cartons": 1, "packs": 0, "pieces": 0,
    })
    assert add_line.status_code == 409


def test_reopen_requires_manager_and_reason(client, setup, login_as):
    created = client.post("/api/dispatches", json={
        "dispatch_number": "F-2", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{created['id']}/finalize")

    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    forbidden = client.post(f"/api/dispatches/{created['id']}/reopen", json={"reason": "fix typo"})
    assert forbidden.status_code == 403

    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    no_reason = client.post(f"/api/dispatches/{created['id']}/reopen", json={})
    assert no_reason.status_code == 400

    ok = client.post(f"/api/dispatches/{created['id']}/reopen", json={"reason": "fix typo"})
    assert ok.status_code == 200
    assert ok.get_json()["status"] == "draft"


def test_void_requires_reason(client, setup):
    created = client.post("/api/dispatches", json={
        "dispatch_number": "F-3", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    no_reason = client.post(f"/api/dispatches/{created['id']}/void", json={})
    assert no_reason.status_code == 400
    ok = client.post(f"/api/dispatches/{created['id']}/void", json={"reason": "customer cancelled order"})
    assert ok.status_code == 200
    assert ok.get_json()["status"] == "void"


def test_voided_dispatch_number_can_be_reused(client, setup):
    first = client.post("/api/dispatches", json={
        "dispatch_number": "REUSE-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{first['id']}/void", json={"reason": "mistake"})

    second = client.post("/api/dispatches", json={
        "dispatch_number": "REUSE-1", "date": "2026-07-29", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert second.status_code == 201


def test_duplicate_dispatch_copies_lines_with_current_rule(client, setup):
    source = client.post("/api/dispatches", json={
        "dispatch_number": "SRC-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()

    res = client.post(f"/api/dispatches/{source['id']}/duplicate", json={"dispatch_number": "COPY-1"})
    assert res.status_code == 201
    copy = res.get_json()
    assert copy["status"] == "draft"
    assert copy["dispatch_number"] == "COPY-1"
    assert copy["lines"][0]["base_unit_qty"] == 200


def test_list_filters_by_status_and_dispatch_number(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "LIST-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.get("/api/dispatches?dispatch_number=LIST-1&status=draft")
    data = res.get_json()
    assert data["total"] == 1
    assert data["results"][0]["dispatch_number"] == "LIST-1"


def test_check_number_endpoint(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "CHK-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.get("/api/dispatches/check-number?number=CHK-1")
    assert res.get_json()["conflict"] is not None
    res2 = client.get("/api/dispatches/check-number?number=NEVER-USED")
    assert res2.get_json()["conflict"] is None


def test_removing_last_line_is_rejected(client, setup):
    created = client.post("/api/dispatches", json={
        "dispatch_number": "LINE-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    line_id = created["lines"][0]["id"]
    res = client.delete(f"/api/dispatches/{created['id']}/lines/{line_id}")
    assert res.status_code == 400
