def test_login_success_is_audited(client, login_as, app):
    login_as("alice", "password123", "manager")

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="login_success").first()
        assert entry is not None
        assert entry.username == "alice"


def test_logout_is_audited(client, login_as, app):
    login_as("alice", "password123", "manager")
    client.post("/api/logout")

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="logout").first()
        assert entry is not None


def test_product_create_is_audited_with_after_state(client, login_as, app):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/products", json={"name": "Audited Widget"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="create", entity_type="product").first()
        assert entry is not None
        assert entry.to_dict()["after"]["name"] == "Audited Widget"


def test_customer_create_is_audited(client, login_as, app):
    login_as("op1", "password123", "operator")
    client.post("/api/admin/customers", json={"name": "Dalca"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="create", entity_type="customer").first()
        assert entry is not None


def test_user_creation_and_role_change_are_audited(client, login_as, app):
    login_as("root", "password123", "super_admin")
    created = client.post("/api/admin/users", json={"username": "newop", "password": "password123", "role": "operator"}).get_json()
    client.patch(f"/api/admin/users/{created['id']}", json={"role": "manager"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        create_entry = AuditLog.query.filter_by(action="create", entity_type="user").first()
        update_entry = AuditLog.query.filter_by(action="update", entity_type="user").first()
        assert create_entry is not None
        assert update_entry is not None
        assert update_entry.to_dict()["before"]["role"] == "operator"
        assert update_entry.to_dict()["after"]["role"] == "manager"


def test_entry_upsert_is_audited(client, login_as, app):
    login_as("op1", "password123", "operator")
    client.post("/api/entries", json={
        "date": "2026-07-28", "shift": "Day", "product": "Lavex",
        "opening": 10, "return_val": 0, "production": 5, "issued": 2, "closing": 13,
    })

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="upsert", entity_type="entry").first()
        assert entry is not None
        assert entry.entity_id == "2026-07-28|Day|Lavex"


def test_csv_export_is_audited(client, login_as, app):
    login_as("viewer1", "password123", "viewer")
    client.get("/api/export.csv")

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="export_csv").first()
        assert entry is not None
        assert entry.username == "viewer1"
