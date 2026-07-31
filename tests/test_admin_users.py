def test_super_admin_can_create_user(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={"username": "newop", "password": "password123", "role": "operator"})
    assert res.status_code == 201
    assert res.get_json()["role"] == "operator"


def test_manager_cannot_manage_users(client, login_as):
    login_as("mgr", "password123", "manager")
    res = client.post("/api/admin/users", json={"username": "newop", "password": "password123", "role": "operator"})
    assert res.status_code == 403


def test_short_password_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={"username": "newop", "password": "short", "role": "operator"})
    assert res.status_code == 400


def test_invalid_role_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={"username": "newop", "password": "password123", "role": "wizard"})
    assert res.status_code == 400


def test_duplicate_username_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/users", json={"username": "dupe", "password": "password123", "role": "operator"})
    res = client.post("/api/admin/users", json={"username": "dupe", "password": "password123", "role": "viewer"})
    assert res.status_code == 409


def test_cannot_deactivate_own_account(client, login_as):
    user = login_as("root", "password123", "super_admin")
    res = client.patch(f"/api/admin/users/{user['id']}", json={"active": False})
    assert res.status_code == 400


def test_cannot_demote_own_account(client, login_as):
    user = login_as("root", "password123", "super_admin")
    res = client.patch(f"/api/admin/users/{user['id']}", json={"role": "viewer"})
    assert res.status_code == 400


def test_deactivated_user_loses_access(client, login_as):
    login_as("root", "password123", "super_admin")
    created = client.post("/api/admin/users", json={"username": "temp", "password": "password123", "role": "operator"}).get_json()
    client.delete(f"/api/admin/users/{created['id']}")
    client.post("/api/logout")

    res = client.post("/api/login", json={"username": "temp", "password": "password123"})
    assert res.status_code == 401


def test_reset_password(client, login_as):
    # Stage 6 moved password changes out of the generic PATCH endpoint and
    # into their own dedicated POST .../reset-password endpoint — see
    # tests/test_stage6_app_shell.py for the full reset-password/session-
    # invalidation/self-lockout coverage this stage added.
    login_as("root", "password123", "super_admin")
    created = client.post("/api/admin/users", json={"username": "temp", "password": "password123", "role": "operator"}).get_json()
    client.post(f"/api/admin/users/{created['id']}/reset-password",
                json={"password": "brandnewpassword", "confirm_password": "brandnewpassword"})
    client.post("/api/logout")

    res = client.post("/api/login", json={"username": "temp", "password": "brandnewpassword"})
    assert res.status_code == 200
