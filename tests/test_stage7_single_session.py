"""
Stage 7 section 2: one active login session per user account. Reuses the
existing session_version mechanism (previously bumped only on a
super-admin password reset — see tests/test_stage6_app_shell.py) rather
than a second, competing session system: every successful login now also
bumps it, so a prior session's stamped value stops matching and its very
next request is treated as logged-out.
"""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")


def test_first_login_succeeds(client, login_as):
    user = login_as("alice", "password123", "manager")
    assert user["username"] == "alice"


def test_second_login_for_same_user_succeeds_and_invalidates_first(app, client, login_as):
    login_as("alice", "password123", "manager")
    assert client.get("/api/session").get_json()["authed"] is True

    other_client = app.test_client()
    res = other_client.post("/api/login", json={"username": "alice", "password": "password123"})
    assert res.status_code == 200
    assert other_client.get("/api/session").get_json()["authed"] is True

    # The FIRST client's session is now superseded.
    assert client.get("/api/session").get_json()["authed"] is False


def test_first_session_rejected_with_specific_message_after_second_login(app, client, login_as):
    login_as("alice", "password123", "manager")
    other_client = app.test_client()
    other_client.post("/api/login", json={"username": "alice", "password": "password123"})

    res = client.get("/api/session")
    data = res.get_json()
    assert data["authed"] is False
    assert data.get("session_superseded") is True
    assert "signed in on another device" in data.get("message", "").lower()


def test_first_session_gets_401_with_superseded_message_on_a_protected_route(app, client, login_as):
    login_as("root", "password123", "super_admin")
    other_client = app.test_client()
    other_client.post("/api/login", json={"username": "root", "password": "password123"})

    res = client.get("/api/admin/users")
    assert res.status_code == 401
    assert "signed in on another device" in res.get_json()["error"].lower()
    assert res.get_json().get("session_superseded") is True


def test_failed_login_does_not_invalidate_the_valid_session(client, login_as):
    login_as("alice", "password123", "manager")
    bad = client.post("/api/login", json={"username": "alice", "password": "wrongpassword"})
    assert bad.status_code == 401
    # The original session (same client/cookie jar) must still be valid —
    # a failed login attempt from anywhere must never invalidate it.
    assert client.get("/api/session").get_json()["authed"] is True


def test_failed_login_attempt_from_another_client_does_not_affect_the_valid_session(app, client, login_as):
    login_as("alice", "password123", "manager")
    other_client = app.test_client()
    other_client.post("/api/login", json={"username": "alice", "password": "wrongpassword"})
    assert client.get("/api/session").get_json()["authed"] is True


def test_logout_invalidates_current_session(client, login_as):
    login_as("alice", "password123", "manager")
    assert client.get("/api/session").get_json()["authed"] is True
    client.post("/api/logout")
    assert client.get("/api/session").get_json()["authed"] is False


def test_password_reset_still_invalidates_all_older_sessions(app, client, login_as):
    login_as("root", "password123", "super_admin")
    target = client.post("/api/admin/users", json={
        "username": "target_s7", "password": "password123", "role": "operator",
    }).get_json()

    target_client = app.test_client()
    target_client.post("/api/login", json={"username": "target_s7", "password": "password123"})
    assert target_client.get("/api/session").get_json()["authed"] is True

    client.post(f"/api/admin/users/{target['id']}/reset-password",
                json={"password": "newpassword1", "confirm_password": "newpassword1"})

    assert target_client.get("/api/session").get_json()["authed"] is False


def test_different_users_sessions_remain_unaffected_by_each_others_logins(app, client, login_as, make_user):
    login_as("alice", "password123", "manager")
    make_user("bob_s7", "password123", "viewer")

    bob_client = app.test_client()
    bob_client.post("/api/login", json={"username": "bob_s7", "password": "password123"})
    assert bob_client.get("/api/session").get_json()["authed"] is True
    # Alice's own session (a completely different user) is untouched by
    # Bob logging in.
    assert client.get("/api/session").get_json()["authed"] is True


def test_session_version_column_reused_not_a_second_system(app):
    """No second session-tracking table/column was introduced — this is
    the exact same User.session_version column password-reset already
    used, per the spec's explicit 'reuse... rather than creating competing
    session systems' instruction."""
    from webapp.models.user import User
    columns = {c.name for c in User.__table__.columns}
    session_related = {c for c in columns if "session" in c.lower()}
    assert session_related == {"session_version"}


def test_login_increments_session_version_not_just_sets_it(app, client, login_as):
    from webapp.models.user import User
    login_as("alice", "password123", "manager")
    with app.app_context():
        v1 = User.query.filter_by(username="alice").first().session_version
    client.post("/api/logout")
    client.post("/api/login", json={"username": "alice", "password": "password123"})
    with app.app_context():
        v2 = User.query.filter_by(username="alice").first().session_version
    assert v2 == v1 + 1


def test_app_shell_shows_superseded_message_and_does_not_alert_repeatedly():
    assert "SUPERSEDED_ALERT_KEY" in APP_SHELL_JS
    assert "session_superseded" in APP_SHELL_JS
    assert "function warnSessionSuperseded(" in APP_SHELL_JS
    idx = APP_SHELL_JS.index("function warnSessionSuperseded(")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "sessionStorage" in body


def test_app_shell_never_shows_superseded_message_for_a_plain_logged_out_session():
    # warnSessionSuperseded() (which alerts) is only ever reached through
    # the session_superseded branch — a plain "never authenticated on this
    # device" session (no session_superseded flag at all) takes the sibling
    # else-branch instead, which redirects to "/" WITHOUT calling
    # warnSessionSuperseded() and therefore without ever alerting.
    idx = APP_SHELL_JS.index("var session = await apiGet('/api/session');")
    end = APP_SHELL_JS.index("return; // login screen", idx)
    body = APP_SHELL_JS[idx:end]
    assert "if (session && session.session_superseded) {" in body
    assert "warnSessionSuperseded(session.message);" in body
    else_branch = body[body.index("} else if ("):]
    assert "warnSessionSuperseded(" not in else_branch  # not even mentioned — the plain-unauthenticated path never alerts
