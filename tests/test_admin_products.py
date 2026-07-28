def test_super_admin_can_create_product(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/products", json={"name": "Test Widget"})
    assert res.status_code == 201
    assert res.get_json()["name"] == "Test Widget"


def test_manager_cannot_create_product(client, login_as):
    login_as("mgr", "password123", "manager")
    res = client.post("/api/admin/products", json={"name": "Test Widget"})
    assert res.status_code == 403


def test_duplicate_product_name_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/products", json={"name": "Widget"})
    res = client.post("/api/admin/products", json={"name": "Widget"})
    assert res.status_code == 409


def test_rename_preserves_id_for_historical_reference(client, login_as):
    login_as("root", "password123", "super_admin")
    create_res = client.post("/api/admin/products", json={"name": "Old Name"})
    product_id = create_res.get_json()["id"]

    rename_res = client.patch(f"/api/admin/products/{product_id}", json={"name": "New Name"})
    assert rename_res.status_code == 200
    assert rename_res.get_json()["id"] == product_id
    assert rename_res.get_json()["name"] == "New Name"


def test_deactivate_product(client, login_as):
    login_as("root", "password123", "super_admin")
    product_id = client.post("/api/admin/products", json={"name": "Widget"}).get_json()["id"]
    res = client.patch(f"/api/admin/products/{product_id}", json={"active": False})
    assert res.get_json()["active"] is False

    listed = client.get("/api/admin/products").get_json()
    assert not any(p["id"] == product_id for p in listed)
    listed_all = client.get("/api/admin/products?include_inactive=1").get_json()
    assert any(p["id"] == product_id for p in listed_all)


def test_packaging_rule_requires_exactly_one_shape(client, login_as):
    login_as("root", "password123", "super_admin")
    product_id = client.post("/api/admin/products", json={"name": "Widget"}).get_json()["id"]

    neither = client.post(f"/api/admin/products/{product_id}/packaging-rules", json={})
    assert neither.status_code == 400

    both = client.post(f"/api/admin/products/{product_id}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10, "carton_to_pieces": 60,
    })
    assert both.status_code == 400


def test_packaging_rule_versioning_closes_previous_rule(client, login_as):
    login_as("root", "password123", "super_admin")
    product_id = client.post("/api/admin/products", json={"name": "Widget"}).get_json()["id"]

    first = client.post(f"/api/admin/products/{product_id}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    }).get_json()
    assert first["effective_to"] is None

    second = client.post(f"/api/admin/products/{product_id}/packaging-rules", json={
        "cartons_to_packs": 5, "packs_to_pieces": 20,
    }).get_json()

    history = client.get(f"/api/admin/products/{product_id}/packaging-rules").get_json()
    closed = next(r for r in history if r["id"] == first["id"])
    current = next(r for r in history if r["id"] == second["id"])
    assert closed["effective_to"] is not None
    assert current["effective_to"] is None

    product = next(p for p in client.get("/api/admin/products").get_json() if p["id"] == product_id)
    assert product["packaging_rule"]["id"] == second["id"]
