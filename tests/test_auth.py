def test_login_success_sets_session_and_returns_user(client, make_user):
    make_user("alice", "correct-horse", "manager")
    res = client.post("/api/login", json={"username": "alice", "password": "correct-horse"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["user"]["username"] == "alice"
    assert data["user"]["role"] == "manager"

    session_res = client.get("/api/session")
    assert session_res.get_json()["authed"] is True


def test_login_wrong_password_rejected(client, make_user):
    make_user("alice", "correct-horse", "manager")
    res = client.post("/api/login", json={"username": "alice", "password": "wrong"})
    assert res.status_code == 401
    assert res.get_json()["ok"] is False

    session_res = client.get("/api/session")
    assert session_res.get_json()["authed"] is False


def test_login_unknown_username_rejected(client):
    res = client.post("/api/login", json={"username": "ghost", "password": "whatever"})
    assert res.status_code == 401


def test_inactive_user_cannot_login(client, make_user):
    make_user("bob", "password123", "operator", active=False)
    res = client.post("/api/login", json={"username": "bob", "password": "password123"})
    assert res.status_code == 401


def test_logout_clears_session(client, login_as):
    login_as("alice", "correct-horse", "manager")
    client.post("/api/logout")
    session_res = client.get("/api/session")
    assert session_res.get_json()["authed"] is False


def test_seed_super_admin_from_env(app, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERADMIN_USERNAME", "root")
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "seed-password-123")

    from webapp.auth import seed_super_admin
    from webapp.models.user import ROLE_SUPER_ADMIN, User

    with app.app_context():
        seed_super_admin()
        admin = User.query.filter_by(username="root").first()
        assert admin is not None
        assert admin.role == ROLE_SUPER_ADMIN


def test_seed_super_admin_skipped_when_users_exist(app, make_user, monkeypatch):
    make_user("existing", "password123", "viewer")
    monkeypatch.setenv("SUPERADMIN_USERNAME", "root")
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "seed-password-123")

    from webapp.auth import seed_super_admin
    from webapp.models.user import User

    with app.app_context():
        seed_super_admin()
        assert User.query.filter_by(username="root").first() is None


def test_failed_login_is_audited(client, make_user, app):
    make_user("alice", "correct-horse", "manager")
    client.post("/api/login", json={"username": "alice", "password": "wrong"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="login_failed").first()
        assert entry is not None
        assert entry.entity_id == "alice"


# ---------- role gating on the legacy entries API ----------

def test_viewer_cannot_write_entries(client, login_as):
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/entries", json={
        "date": "2026-07-28", "shift": "Day", "product": "Lavex",
        "opening": 10, "return_val": 0, "production": 0, "issued": 0, "closing": 10,
    })
    assert res.status_code == 403


def test_operator_can_write_entries(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.post("/api/entries", json={
        "date": "2026-07-28", "shift": "Day", "product": "Lavex",
        "opening": 10, "return_val": 0, "production": 0, "issued": 0, "closing": 10,
    })
    assert res.status_code == 200


def test_viewer_can_read_entries(client, login_as):
    login_as("viewer1", "password123", "viewer")
    res = client.get("/api/entries")
    assert res.status_code == 200


def test_anonymous_cannot_read_entries(client):
    res = client.get("/api/entries")
    assert res.status_code == 401
