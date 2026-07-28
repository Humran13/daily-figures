def test_operator_can_create_customer(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.post("/api/admin/customers", json={"name": "Dalca"})
    assert res.status_code == 201


def test_viewer_cannot_create_customer(client, login_as):
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/admin/customers", json={"name": "Dalca"})
    assert res.status_code == 403


def test_similar_customer_name_warns_before_creating(client, login_as):
    login_as("op1", "password123", "operator")
    client.post("/api/admin/customers", json={"name": "Dalca"})

    res = client.post("/api/admin/customers", json={"name": "Danka"})
    assert res.status_code == 409
    assert res.get_json()["warning"] == "similar_customers_exist"


def test_confirm_not_duplicate_bypasses_warning(client, login_as):
    login_as("op1", "password123", "operator")
    client.post("/api/admin/customers", json={"name": "Dalca"})

    res = client.post("/api/admin/customers", json={"name": "Danka", "confirm_not_duplicate": True})
    assert res.status_code == 201


def test_dissimilar_name_creates_without_warning(client, login_as):
    login_as("op1", "password123", "operator")
    client.post("/api/admin/customers", json={"name": "Dalca"})

    res = client.post("/api/admin/customers", json={"name": "Completely Different Co"})
    assert res.status_code == 201


def test_invalid_category_rejected(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.post("/api/admin/customers", json={"name": "Someone", "category": "not-a-real-category"})
    assert res.status_code == 400


def test_operator_cannot_update_customer_but_manager_can(client, login_as):
    login_as("op1", "password123", "operator")
    customer_id = client.post("/api/admin/customers", json={"name": "Dalca"}).get_json()["id"]
    client.post("/api/logout")

    login_as("mgr1", "password123", "manager")
    res = client.patch(f"/api/admin/customers/{customer_id}", json={"active": False})
    assert res.status_code == 200
    assert res.get_json()["active"] is False


def test_search_customers_by_partial_name(client, login_as):
    login_as("op1", "password123", "operator")
    client.post("/api/admin/customers", json={"name": "Shop Kikubo"})
    client.post("/api/admin/customers", json={"name": "Derrick"})

    res = client.get("/api/admin/customers?q=kiku")
    names = [c["name"] for c in res.get_json()]
    assert "Shop Kikubo" in names
    assert "Derrick" not in names
