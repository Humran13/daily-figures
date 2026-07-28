import pytest


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def categories(client, super_admin):
    """The real migration seeds these 5 categories; the test `app` fixture
    uses db.create_all() instead, so tests must create them explicitly."""
    for name in ["Corporate Sales", "Metro Sales", "Upcountry Sales", "Shop/Kikuubo Sales", "Factory Sales"]:
        client.post("/api/admin/sales-categories", json={"name": name})
    return True


def test_corporate_preview_is_read_only(client, categories, app):
    res = client.get("/api/admin/recipient-import/corporate-sales/preview")
    assert res.status_code == 200
    data = res.get_json()
    assert data["total_supplied"] == 104
    assert len(data["to_create"]) == 104  # nothing exists yet

    from webapp.models.customer import Customer
    with app.app_context():
        assert Customer.query.count() == 0  # preview must not write anything


def test_corporate_preview_flags_kenjoy_supermrket_typo(client, categories):
    data = client.get("/api/admin/recipient-import/corporate-sales/preview").get_json()
    flagged_names = [v["name"] for v in data["possible_spelling_variations"]]
    assert "Kenjoy Supermrket Nansana" in flagged_names or "Kenjoy Supermrket Mengo" in flagged_names


def test_corporate_execute_creates_104_recipients_under_corporate_sales(client, categories, app):
    res = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    assert res.status_code == 201
    data = res.get_json()
    assert data["created_count"] == 104
    assert data["category"]["name"] == "Corporate Sales"

    from webapp.models.customer import Customer
    from webapp.models.sales_category import SalesCategory
    with app.app_context():
        category = SalesCategory.query.filter_by(name="Corporate Sales").first()
        assert category is not None
        count = Customer.query.filter_by(sales_category_id=category.id).count()
        assert count == 104


def test_corporate_execute_is_idempotent(client, categories):
    first = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True}).get_json()
    assert first["created_count"] == 104

    second = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True}).get_json()
    assert second["created_count"] == 0
    assert second["skipped_count"] == 104


def test_corporate_execute_preserves_branch_specific_recipients_as_separate(client, categories, app):
    client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    from webapp.models.customer import Customer
    with app.app_context():
        names = {c.name for c in Customer.query.all()}
        # explicitly-called-out branch pairs must both exist as distinct rows
        assert "Carrefour Oasis Mall" in names and "Carrefour Lugogo Mall" in names
        assert "Capital Shoppers Ntinda" in names and "Capital Shoppers Nakawa" in names
        assert "Fraine Supermarket Kiira" in names and "Fraine Supermarket Ntinda" in names
        assert "Standard Supermarket Old Park" in names and "Standard Supermarket Garden City" in names
        assert "Portbell Supermarket" in names and "Portbell Supermarket Kireka" in names
        assert {"Kenjoy Supermarket", "Kenjoy Supermarket Najjanakumbi",
                "Kenjoy Supermrket Nansana", "Kenjoy Supermrket Mengo"}.issubset(names)


def test_corporate_execute_does_not_correct_the_supermrket_typo(client, categories, app):
    client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    from webapp.models.customer import Customer
    with app.app_context():
        assert Customer.query.filter_by(name="Kenjoy Supermrket Nansana").first() is not None
        assert Customer.query.filter_by(name="Kenjoy Supermarket Nansana").first() is None


def test_corporate_execute_skips_manually_pre_created_exact_name(client, categories, app):
    """Idempotency must also hold if an admin manually created one of the
    104 names beforehand — no duplicate, and the pre-existing row is untouched."""
    corporate = next(c for c in client.get("/api/admin/sales-categories").get_json() if c["name"] == "Corporate Sales")
    pre_existing = client.post("/api/admin/customers", json={
        "name": "Shopwise Retail LTD", "sales_category_id": corporate["id"], "notes": "manually entered first",
    }).get_json()

    result = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True}).get_json()
    assert "Shopwise Retail LTD" in result["skipped_names"]

    from webapp.models.customer import Customer
    with app.app_context():
        matches = Customer.query.filter_by(name="Shopwise Retail LTD").all()
        assert len(matches) == 1
        assert matches[0].id == pre_existing["id"]
        assert matches[0].notes == "manually entered first"  # untouched


def test_corporate_import_requires_super_admin(client, login_as):
    login_as("mgr", "password123", "manager")
    res = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    assert res.status_code == 403

    res2 = client.get("/api/admin/recipient-import/corporate-sales/preview")
    assert res2.status_code == 403


def test_execute_without_confirm_is_rejected_and_creates_nothing(client, categories, app):
    """The explicit confirmation step is enforced server-side, not just by
    the frontend's confirm() dialog."""
    res = client.post("/api/admin/recipient-import/corporate-sales/execute")  # no body at all
    assert res.status_code == 400
    assert "confirmation" in res.get_json()["error"]

    res2 = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": False})
    assert res2.status_code == 400

    from webapp.models.customer import Customer
    with app.app_context():
        assert Customer.query.count() == 0


def test_execute_batch_raises_before_writing_anything_for_unknown_category(app):
    """The category lookup happens before any Customer row is built, so a
    bad category name never leaves a partial batch behind."""
    with app.app_context():
        from webapp.models.customer import Customer
        from webapp.services.recipient_import_service import RecipientImportError, execute_batch

        with pytest.raises(RecipientImportError):
            execute_batch(["Some Name"], "Category That Does Not Exist", None)
        assert Customer.query.count() == 0


def test_import_route_rejects_missing_category_cleanly(client, login_as):
    """Without the sales categories ever having been created, the import
    routes fail with a clear 400 rather than a server error."""
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    assert res.status_code == 400


# ---------- initial assignments ----------

def test_initial_assignments_preview_groups_by_category(client, categories):
    data = client.get("/api/admin/recipient-import/initial-assignments/preview").get_json()
    categories = {p["category"]["name"] for p in data}
    assert categories == {"Metro Sales", "Upcountry Sales", "Shop/Kikuubo Sales"}


def test_initial_assignments_execute_creates_correct_categories(client, categories, app):
    res = client.post("/api/admin/recipient-import/initial-assignments/execute", json={"confirm": True})
    assert res.status_code == 201

    from webapp.models.customer import Customer
    with app.app_context():
        dakar = Customer.query.filter_by(name="Dakar").first()
        ayub = Customer.query.filter_by(name="Ayub").first()
        shop = Customer.query.filter_by(name="Shop").first()
        assert dakar.sales_category.name == "Metro Sales"
        assert ayub.sales_category.name == "Upcountry Sales"
        assert shop.sales_category.name == "Shop/Kikuubo Sales"


def test_initial_assignments_does_not_create_under_corporate(client, categories, app):
    client.post("/api/admin/recipient-import/initial-assignments/execute", json={"confirm": True})
    from webapp.models.customer import Customer
    with app.app_context():
        for name in ("Dakar", "Derrick", "Ayub", "Fenecansi", "Shop"):
            customer = Customer.query.filter_by(name=name).first()
            assert customer.sales_category.name != "Corporate Sales"


def test_initial_assignments_checks_for_existing_customer_first(client, categories, app):
    metro = next(c for c in client.get("/api/admin/sales-categories").get_json() if c["name"] == "Metro Sales")
    pre_existing = client.post("/api/admin/customers", json={"name": "Dakar", "sales_category_id": metro["id"]}).get_json()

    result = client.post("/api/admin/recipient-import/initial-assignments/execute", json={"confirm": True}).get_json()
    metro_result = next(r for r in result if r["category"]["name"] == "Metro Sales")
    assert "Dakar" in metro_result["skipped_names"]

    from webapp.models.customer import Customer
    with app.app_context():
        assert Customer.query.filter_by(name="Dakar").count() == 1


def test_import_execution_is_audited(client, categories, app):
    client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True})
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="import_execute", entity_type="customer").first()
        assert entry is not None


def test_import_preview_is_audited(client, categories, app):
    client.get("/api/admin/recipient-import/corporate-sales/preview")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="import_preview", entity_type="customer").first()
        assert entry is not None
