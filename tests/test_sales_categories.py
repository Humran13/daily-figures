import pytest


def test_categories_are_seeded_by_migration_not_test_fixture(client, login_as):
    """The `app` test fixture uses db.create_all(), not the migration seed —
    so categories must be created explicitly in tests. This just documents
    that expectation; the real seeding is covered in test_migrations."""
    login_as("root", "password123", "super_admin")
    res = client.get("/api/admin/sales-categories")
    assert res.status_code == 200


def test_super_admin_can_create_category(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/sales-categories", json={"name": "Regional Sales"})
    assert res.status_code == 201
    assert res.get_json()["name"] == "Regional Sales"


def test_manager_cannot_create_category(client, login_as):
    login_as("mgr", "password123", "manager")
    res = client.post("/api/admin/sales-categories", json={"name": "Regional Sales"})
    assert res.status_code == 403


def test_operator_cannot_create_category(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.post("/api/admin/sales-categories", json={"name": "Regional Sales"})
    assert res.status_code == 403


def test_duplicate_category_name_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/sales-categories", json={"name": "Metro Sales"})
    res = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"})
    assert res.status_code == 409


def test_rename_category_preserves_id_and_dispatch_links(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Old Name"}).get_json()
    product = client.post("/api/admin/products", json={"name": "Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    customer = client.post("/api/admin/customers", json={
        "name": "Test Recipient", "sales_category_id": category["id"],
    }).get_json()

    dispatch = client.post("/api/dispatches", json={
        "dispatch_number": "CAT-1", "date": "2026-07-28", "shift": "Day",
        "customer_id": customer["id"], "sales_category_id": category["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    rename = client.patch(f"/api/admin/sales-categories/{category['id']}", json={"name": "New Name"})
    assert rename.status_code == 200

    fetched = client.get(f"/api/dispatches/{dispatch['id']}").get_json()
    assert fetched["sales_category_id"] == category["id"]
    # historical snapshot captured the name AT THE TIME — a later rename
    # does not silently rewrite it
    assert fetched["sales_category_name_snapshot"] == "Old Name"


def test_deactivate_category_hides_from_default_list(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Temp Category"}).get_json()
    client.patch(f"/api/admin/sales-categories/{category['id']}", json={"active": False})

    active_only = client.get("/api/admin/sales-categories").get_json()
    assert not any(c["id"] == category["id"] for c in active_only)
    with_inactive = client.get("/api/admin/sales-categories?include_inactive=1").get_json()
    assert any(c["id"] == category["id"] for c in with_inactive)
