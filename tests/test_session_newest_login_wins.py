"""
Targeted fix: "Sign in from the main app first." was trapping users with
no way forward, framed as "unable to sign in". Investigation found the
BACKEND session-replacement rule (User.session_version, webapp/auth.py)
was already correct — newest successful login always wins immediately,
never blocked by an existing session (see tests/test_stage7_single_
session.py's extensive pre-existing coverage of that core rule, all
still passing unmodified). The actual bugs were:

1. A narrow race in login()'s session_version increment: two logins for
   the same account arriving close together could both read the same
   "current" value and both write back the same "next" value in Python,
   letting two sessions stay simultaneously valid. Fixed by making the
   increment an atomic `UPDATE ... SET session_version = session_version
   + 1` (webapp/auth.py), the same DB-level-atomic pattern already used
   elsewhere in this codebase (e.g. correction_request_service.
   consume_grant()) — never a second/competing session mechanism.

2. static/app-shell.js's warnSessionSuperseded() only ever redirected to
   "/" the FIRST time it ran per browser tab (guarded by the same flag
   that also gated the one-time alert) — a device that dismissed that
   alert once, then later navigated straight to a gated page again
   (bookmark/PWA shortcut) without ever completing a fresh login, would
   silently stop redirecting: stuck on that page's own static "Sign in
   from the main app first." text, which has no link of its own. Fixed
   by separating "alert once per tab" from "always redirect".

3. app-shell.js's render() never redirected a PLAIN "never authenticated
   on this device at all" session either (only the session_superseded
   case) — e.g. a fresh browser or a bookmark/PWA shortcut pointing
   straight at dispatch.html/returns.html/production.html/history.html
   (which, unlike dashboard.html/requests.html, have no server-side
   auth redirect of their own — see webapp/routes/pages.py's own
   comments on why). Fixed by adding a silent (no alert — this isn't an
   alarming "you were signed out" event) redirect to "/" for that case
   too.

This file covers what test_stage7_single_session.py does not already:
3-way rapid succession, permission preservation across replacement,
stale-session logout safety, same-session/multiple-tab behavior, and the
app-shell.js redirect fix itself. It does not re-test the core
replacement rule (first login succeeds, second succeeds and invalidates
the first, failed logins never invalidate a valid session, different
users are independent) — that is already thoroughly covered and
untouched.
"""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")


# =====================================================================
# SECTION 17 / 13 — rapid succession, three (or more) devices
# =====================================================================

def test_third_login_wins_over_first_and_second(app, make_user):
    make_user("ambrose", "password123", "manager")
    # Three independent cookie jars — login_as() is bound to the shared
    # `client` fixture, so three separate app.test_client() instances are
    # used instead, one per simulated device, each keeping its own
    # cookies independently.
    a = app.test_client()
    b = app.test_client()
    c = app.test_client()
    a.post("/api/login", json={"username": "ambrose", "password": "password123"})
    assert a.get("/api/session").get_json()["authed"] is True

    b.post("/api/login", json={"username": "ambrose", "password": "password123"})
    assert b.get("/api/session").get_json()["authed"] is True
    assert a.get("/api/session").get_json()["authed"] is False  # A superseded by B

    c.post("/api/login", json={"username": "ambrose", "password": "password123"})
    assert c.get("/api/session").get_json()["authed"] is True
    assert a.get("/api/session").get_json()["authed"] is False  # still invalid
    assert b.get("/api/session").get_json()["authed"] is False  # now ALSO invalid


def test_winning_devices_protected_api_request_succeeds(app, login_as):
    login_as("winner_root", "password123", "super_admin")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "winner_root", "password": "password123"})
    b.post("/api/login", json={"username": "winner_root", "password": "password123"})

    # A (superseded) hitting a real protected endpoint is rejected...
    assert a.get("/api/admin/users").status_code == 401
    # ...while B (the winner) succeeds normally on the exact same endpoint.
    assert b.get("/api/admin/users").status_code == 200


def test_session_version_increment_is_an_atomic_update_not_a_python_read_modify_write(app):
    # Confirms the race-safety mechanism itself (section 13) — the exact
    # pattern this codebase already uses elsewhere for the same class of
    # problem (e.g. correction_request_service.consume_grant()), not a
    # new/competing mechanism.
    import inspect
    from webapp import auth as auth_module
    src = inspect.getsource(auth_module.login)
    assert "update(User).where(User.id == user.id).values(session_version=User.session_version + 1)" in src
    assert "db.session.refresh(user)" in src


# =====================================================================
# SECTION 18 — failed login variants must never invalidate the winner
# =====================================================================

def test_wrong_username_attempt_does_not_invalidate_the_valid_session(app, login_as):
    login_as("safe_user", "password123", "manager")
    a = app.test_client()
    a.post("/api/login", json={"username": "safe_user", "password": "password123"})
    assert a.get("/api/session").get_json()["authed"] is True

    b = app.test_client()
    res = b.post("/api/login", json={"username": "totally_nonexistent_user", "password": "whatever"})
    assert res.status_code == 401
    assert a.get("/api/session").get_json()["authed"] is True


def test_inactive_account_login_attempt_does_not_invalidate_a_different_valid_session(app, login_as):
    login_as("iv_root", "password123", "super_admin")
    root = app.test_client()
    root.post("/api/login", json={"username": "iv_root", "password": "password123"})

    created = root.post("/api/admin/users", json={
        "username": "iv_target", "password": "password123", "role": "viewer",
    }).get_json()
    valid_target = app.test_client()
    valid_target.post("/api/login", json={"username": "iv_target", "password": "password123"})
    assert valid_target.get("/api/session").get_json()["authed"] is True

    # Deactivate the account, then attempt to log in as it again.
    root.patch(f"/api/admin/users/{created['id']}", json={"active": False})
    forged = app.test_client()
    res = forged.post("/api/login", json={"username": "iv_target", "password": "password123"})
    assert res.status_code == 401

    # root's own unrelated session is completely unaffected either way.
    assert root.get("/api/session").get_json()["authed"] is True


# =====================================================================
# SECTION 19 — stale/superseded session logout safety
# =====================================================================

def test_stale_session_logout_does_not_invalidate_the_replacing_session(app, login_as):
    login_as("logout_safety_user", "password123", "manager")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "logout_safety_user", "password": "password123"})
    b.post("/api/login", json={"username": "logout_safety_user", "password": "password123"})
    assert a.get("/api/session").get_json()["authed"] is False  # already superseded

    # The stale device calls logout anyway (e.g. a queued/leftover request).
    logout_res = a.post("/api/logout")
    assert logout_res.status_code == 200  # never errors — just a no-op for an already-invalid session

    # B, the real current session, is completely unaffected.
    assert b.get("/api/session").get_json()["authed"] is True

    # B can still log out normally afterward, on its own terms.
    assert b.post("/api/logout").status_code == 200
    assert b.get("/api/session").get_json()["authed"] is False


def test_stale_session_logout_creates_no_audit_entry(app, login_as):
    # current_user() is None for a superseded session, so logout()'s own
    # `if user is not None: record_audit(...)` guard never fires — no
    # false logout event is recorded for a session that wasn't really
    # the current one.
    login_as("logout_audit_user", "password123", "manager")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "logout_audit_user", "password": "password123"})
    b.post("/api/login", json={"username": "logout_audit_user", "password": "password123"})
    a.post("/api/logout")

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        from webapp.models.user import User
        user = User.query.filter_by(username="logout_audit_user").first()
        logout_entries = AuditLog.query.filter_by(action="logout", entity_type="user", entity_id=str(user.id)).all()
        # Only ever the real logouts that actually happen in this test
        # (none yet) — the stale device's no-op logout must not appear.
        assert len(logout_entries) == 0


# =====================================================================
# SECTION 20 — same session / multiple tabs
# =====================================================================

def test_requests_sharing_the_same_cookie_jar_both_remain_valid(app, login_as):
    # Flask's test client is itself the "one browser" whose cookie jar
    # persists across calls — two requests through the SAME client
    # instance are exactly what two tabs in the same real browser would
    # send (identical session cookie), and neither one is a competing
    # "device" the single-session rule is meant to reject.
    login_as("tabs_user", "password123", "manager")
    client = app.test_client()
    client.post("/api/login", json={"username": "tabs_user", "password": "password123"})

    first_tab = client.get("/api/session")
    second_tab = client.get("/api/session")
    assert first_tab.get_json()["authed"] is True
    assert second_tab.get_json()["authed"] is True


def test_multiple_tabs_do_not_supersede_each_other(app, login_as):
    login_as("tabs_user2", "password123", "manager")
    client = app.test_client()
    client.post("/api/login", json={"username": "tabs_user2", "password": "password123"})
    # Simulates several tabs each independently hitting a read endpoint —
    # none of these are logins, so none of them can supersede anything.
    for _ in range(5):
        assert client.get("/api/session").get_json()["authed"] is True


# =====================================================================
# SECTION 21 — permissions preserved across session replacement
# =====================================================================

def test_permissions_unchanged_after_replacement_operator(app, login_as):
    login_as("perm_op", "password123", "operator")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "perm_op", "password": "password123"})
    b.post("/api/login", json={"username": "perm_op", "password": "password123"})
    # Operator never had Dashboard access — unchanged, on the winning session.
    assert b.get("/api/dashboard").status_code == 403


def test_permissions_unchanged_after_replacement_viewer(app, login_as):
    login_as("perm_viewer", "password123", "viewer")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "perm_viewer", "password": "password123"})
    b.post("/api/login", json={"username": "perm_viewer", "password": "password123"})
    assert b.get("/api/dashboard").status_code == 200
    assert b.post("/api/dispatches", json={}).status_code == 403


def test_permissions_unchanged_after_replacement_accountant(app, login_as):
    login_as("perm_acct", "password123", "accountant")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "perm_acct", "password": "password123"})
    b.post("/api/login", json={"username": "perm_acct", "password": "password123"})
    assert b.get("/api/correction-requests/pending-count").status_code == 200
    assert b.get("/api/admin/users").status_code == 403


def test_permissions_unchanged_after_replacement_manager(app, login_as):
    login_as("perm_mgr", "password123", "manager")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "perm_mgr", "password": "password123"})
    b.post("/api/login", json={"username": "perm_mgr", "password": "password123"})
    assert b.get("/api/dashboard").status_code == 200
    assert b.post("/api/daily-reset/preview", json={}).status_code == 403  # Reset stays Super Admin only


def test_permissions_unchanged_after_replacement_super_admin(app, login_as):
    login_as("perm_root", "password123", "super_admin")
    a = app.test_client()
    b = app.test_client()
    a.post("/api/login", json={"username": "perm_root", "password": "password123"})
    b.post("/api/login", json={"username": "perm_root", "password": "password123"})
    assert b.get("/api/admin/users").status_code == 200


# =====================================================================
# SECTION 22 — the observed "Sign in from the main app first." dead end
# =====================================================================

def test_gate_message_still_used_only_as_a_static_no_js_fallback():
    # The message itself is preserved (still a legitimate first-paint
    # placeholder before JS runs) — this proves it's no longer the ONLY
    # thing that happens: app-shell.js now actively redirects away from
    # it in every real (JS-enabled) case, superseded or not.
    from pathlib import Path
    for name in ("dashboard.html", "dispatch.html", "history.html", "production.html", "requests.html", "returns.html"):
        html = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert "Sign in from the main app first." in html, name
        assert 'src="/app-shell.js"' in html, name


def test_app_shell_redirects_plain_unauthenticated_sessions_to_login():
    idx = APP_SHELL_JS.index("var session = await apiGet('/api/session');")
    body = APP_SHELL_JS[idx:idx + 1200]
    assert "location.href = '/';" in body
    assert "Never authenticated on THIS device at all" in body


def test_app_shell_plain_unauthenticated_redirect_never_alerts():
    idx = APP_SHELL_JS.index("} else if (location.pathname !== '/' && location.pathname !== '/index.html') {")
    end = APP_SHELL_JS.index("\n      }", idx)
    body = APP_SHELL_JS[idx:end]
    assert "warnSessionSuperseded" not in body
    assert "window.alert" not in body


def test_app_shell_superseded_redirect_no_longer_gated_behind_the_once_per_tab_alert_flag():
    idx = APP_SHELL_JS.index("function warnSessionSuperseded(message) {")
    end = APP_SHELL_JS.index("\n  }", idx)
    body = APP_SHELL_JS[idx:end]
    # The redirect line must sit OUTSIDE the try/sessionStorage-guarded
    # alert block — i.e. after its closing brace — so it runs on every
    # call, not just the first one per tab.
    redirect_idx = body.index("location.href = '/';")
    catch_close_idx = body.rindex("}", 0, redirect_idx)
    assert catch_close_idx < redirect_idx
    assert body.count("location.href = '/';") == 1


def test_login_page_itself_is_never_redirect_looped():
    # Both warnSessionSuperseded() and the plain-unauthenticated branch
    # explicitly exclude "/" and "/index.html" from the redirect target,
    # so landing on the login page never bounces back to itself.
    assert APP_SHELL_JS.count("location.pathname !== '/' && location.pathname !== '/index.html'") == 2
