import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    metro = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    corporate = client.post("/api/admin/sales-categories", json={"name": "Corporate Sales"}).get_json()
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    dakar = client.post("/api/admin/customers", json={"name": "Dakar", "sales_category_id": metro["id"]}).get_json()
    shopwise = client.post("/api/admin/customers", json={"name": "Shopwise Retail LTD", "sales_category_id": corporate["id"]}).get_json()
    return {"metro": metro, "corporate": corporate, "product": product, "dakar": dakar, "shopwise": shopwise}


# ---------- category-first recipient filtering ----------

def test_recipient_list_scoped_to_category(client, setup):
    metro_results = client.get(f"/api/admin/customers?sales_category_id={setup['metro']['id']}").get_json()
    assert any(c["name"] == "Dakar" for c in metro_results)
    assert not any(c["name"] == "Shopwise Retail LTD" for c in metro_results)

    corp_results = client.get(f"/api/admin/customers?sales_category_id={setup['corporate']['id']}").get_json()
    assert any(c["name"] == "Shopwise Retail LTD" for c in corp_results)
    assert not any(c["name"] == "Dakar" for c in corp_results)


def test_recipient_autocomplete_partial_match_within_category(client, setup):
    res = client.get(f"/api/admin/customers?sales_category_id={setup['corporate']['id']}&q=shopwise").get_json()
    assert any(c["name"] == "Shopwise Retail LTD" for c in res)


# ---------- dispatch creation: category/recipient must match ----------

def test_dispatch_with_matching_category_and_customer_succeeds(client, setup):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-1", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "customer_id": setup["dakar"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201
    data = res.get_json()
    assert data["sales_category_name_snapshot"] == "Metro Sales"
    assert data["customer_name_snapshot"] == "Dakar"


def test_dispatch_rejects_recipient_from_wrong_category(client, setup):
    """A recipient must never be silently saved under the wrong category."""
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-2", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "customer_id": setup["shopwise"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400
    assert "different sales category" in res.get_json()["error"]


def test_dispatch_without_explicit_category_derives_from_customer(client, setup):
    """Omitting sales_category_id is fine as long as the recipient already
    has one — it's derived, not left blank."""
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-3", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["dakar"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201
    assert res.get_json()["sales_category_id"] == setup["metro"]["id"]


def test_new_dispatch_rejected_when_no_category_available_at_all(client, setup):
    """Every NEW dispatch must have a category — an uncategorized customer
    with no explicit category given must be rejected, never silently null."""
    uncategorized = client.post("/api/admin/customers", json={"name": "No Category Customer"}).get_json()
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-NOCAT", "date": "2026-07-28", "shift": "Day",
        "customer_id": uncategorized["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400
    assert "sales category is required" in res.get_json()["error"]


# ---------- Other / New Customer (temporary recipient) ----------

def test_dispatch_creates_temporary_customer_inline(client, setup, app):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-4", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "new_customer_name": "Walk-in Customer",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201
    data = res.get_json()
    assert data["customer_name_snapshot"] == "Walk-in Customer"

    from webapp.models.customer import Customer
    with app.app_context():
        customer = Customer.query.filter_by(name="Walk-in Customer").first()
        assert customer is not None
        assert customer.is_temporary is True
        assert customer.sales_category_id == setup["metro"]["id"]


def test_temporary_customer_creation_is_audited(client, setup, app):
    client.post("/api/dispatches", json={
        "dispatch_number": "RW-5", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "new_customer_name": "Unknown Customer",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="create_temporary", entity_type="customer").first()
        assert entry is not None


def test_new_customer_name_requires_category(client, setup):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-6", "date": "2026-07-28", "shift": "Day",
        "new_customer_name": "Some New Name",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


def test_similar_name_warning_does_not_block_temporary_creation(client, setup):
    """The operator must be able to continue with a temporary name even if
    it's similar to an existing recipient — never silently blocked."""
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-7", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "new_customer_name": "Dakr",  # similar to "Dakar"
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201  # succeeds despite the similarity — just flagged for later review


def test_check_duplicate_surfaces_similar_names_for_frontend_warning(client, setup):
    res = client.get("/api/admin/customers/check-duplicate?name=Dakr")
    matches = res.get_json()["matches"]
    assert any(m["name"] == "Dakar" for m in matches)


# ---------- viewer/permission boundaries ----------

def test_viewer_cannot_create_dispatch_with_category(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RW-8", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["metro"]["id"], "customer_id": setup["dakar"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403
