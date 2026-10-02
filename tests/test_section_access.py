"""
Store Department / Section Access: Finished Goods + Spare Parts. Section
access answers WHERE a user may log in; it is layered on top of the
existing newest-login-wins session mechanism (webapp/auth.py) and the
existing role system (webapp/models/user.py), neither of which this
feature changes. See the migration
migrations/versions/d1e3f5a7b9c2_add_departments_sections.py and
webapp/services/section_access_service.py.
"""
from webapp.extensions import db
from webapp.models.section import Section, SECTION_FINISHED_GOODS, SECTION_SPARE_PARTS
from webapp.models.user import User
from webapp.services import section_access_service


def _grant(app, username, codes):
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        section_access_service.set_sections_for_user(user, codes, user)
        db.session.commit()


# ---------- GET /api/sections (public, pre-login) ----------

def test_sections_endpoint_is_public_and_lists_store_department(client):
    res = client.get("/api/sections")
    assert res.status_code == 200
    data = res.get_json()
    codes = [d["code"] for d in data["departments"]]
    assert "store" in codes
    store = next(d for d in data["departments"] if d["code"] == "store")
    section_codes = {s["code"] for s in store["sections"]}
    assert section_codes == {"finished_goods", "spare_parts"}


# ---------- login defaults / explicit section ----------

def test_login_without_section_field_defaults_to_finished_goods(client, make_user):
    make_user("nosec", "password123", "operator")
    res = client.post("/api/login", json={"username": "nosec", "password": "password123"})
    assert res.status_code == 200
    assert client.get("/api/session").get_json()["active_section"] == "finished_goods"


def test_login_explicit_finished_goods_succeeds_for_default_user(client, make_user):
    make_user("fguser", "password123", "manager")
    res = client.post("/api/login", json={"username": "fguser", "password": "password123", "section": "finished_goods"})
    assert res.status_code == 200


def test_login_spare_parts_rejected_for_user_with_only_finished_goods(client, make_user):
    make_user("fgonly", "password123", "manager")
    res = client.post("/api/login", json={"username": "fgonly", "password": "password123", "section": "spare_parts"})
    assert res.status_code == 403
    assert "access" in res.get_json()["error"].lower()


def test_login_unknown_section_rejected(client, make_user):
    make_user("unk", "password123", "manager")
    res = client.post("/api/login", json={"username": "unk", "password": "password123", "section": "corporate"})
    assert res.status_code == 400


def test_login_with_spare_parts_grant_succeeds(app, client, make_user):
    make_user("spuser", "password123", "operator")
    _grant(app, "spuser", ["spare_parts"])
    res = client.post("/api/login", json={"username": "spuser", "password": "password123", "section": "spare_parts"})
    assert res.status_code == 200
    assert client.get("/api/session").get_json()["active_section"] == "spare_parts"


def test_login_with_spare_parts_grant_still_rejected_for_finished_goods(app, client, make_user):
    make_user("sponly", "password123", "operator")
    _grant(app, "sponly", ["spare_parts"])
    res = client.post("/api/login", json={"username": "sponly", "password": "password123", "section": "finished_goods"})
    assert res.status_code == 403


def test_user_with_both_sections_may_log_into_either(app, client, make_user):
    make_user("bothuser", "password123", "manager")
    _grant(app, "bothuser", ["finished_goods", "spare_parts"])
    a = client.post("/api/login", json={"username": "bothuser", "password": "password123", "section": "finished_goods"})
    assert a.status_code == 200
    client.post("/api/logout")
    b = client.post("/api/login", json={"username": "bothuser", "password": "password123", "section": "spare_parts"})
    assert b.status_code == 200


# ---------- unauthorized-section attempts must never invalidate a valid session ----------

def test_unauthorized_section_attempt_does_not_invalidate_other_valid_session(app, make_user):
    make_user("safe2", "password123", "manager")  # finished_goods only (default)
    a = app.test_client()
    a.post("/api/login", json={"username": "safe2", "password": "password123", "section": "finished_goods"})
    assert a.get("/api/session").get_json()["authed"] is True

    b = app.test_client()
    res = b.post("/api/login", json={"username": "safe2", "password": "password123", "section": "spare_parts"})
    assert res.status_code == 403
    assert a.get("/api/session").get_json()["authed"] is True  # A untouched


def test_unauthorized_section_attempt_creates_no_authenticated_session(client, make_user):
    make_user("nosess", "password123", "viewer")
    res = client.post("/api/login", json={"username": "nosess", "password": "password123", "section": "spare_parts"})
    assert res.status_code == 403
    assert client.get("/api/session").get_json()["authed"] is False


# ---------- API section isolation (enforce_section before_request) ----------

def test_spare_parts_session_cannot_access_finished_goods_api(app, client, make_user):
    make_user("isoluser", "password123", "manager")
    _grant(app, "isoluser", ["spare_parts"])
    client.post("/api/login", json={"username": "isoluser", "password": "password123", "section": "spare_parts"})
    res = client.get("/api/dashboard")
    assert res.status_code == 403
    assert res.get_json()["error"] == "forbidden_section"


def test_finished_goods_session_can_access_finished_goods_api(client, login_as):
    login_as("fgapi", "password123", "manager")
    assert client.get("/api/dashboard").status_code == 200


def test_unauthenticated_request_to_fg_api_still_gets_existing_401(client):
    res = client.get("/api/dashboard")
    assert res.status_code == 401


# ---------- page guard isolation ----------

def test_wrong_section_direct_page_access_redirects_to_root(app, client, make_user):
    make_user("pageuser", "password123", "manager")
    _grant(app, "pageuser", ["spare_parts"])
    client.post("/api/login", json={"username": "pageuser", "password": "password123", "section": "spare_parts"})
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


def test_spare_parts_page_requires_spare_parts_session(client, login_as):
    login_as("fgpageuser", "password123", "manager")  # finished_goods only
    res = client.get("/spare-parts.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


def test_admin_page_reachable_regardless_of_active_section(app, make_user):
    make_user("adminsp", "password123", "super_admin")
    _grant(app, "adminsp", ["spare_parts"])
    c = app.test_client()
    c.post("/api/login", json={"username": "adminsp", "password": "password123", "section": "spare_parts"})
    res = c.get("/admin.html")
    assert res.status_code == 200


# ---------- admin section-access management ----------

def test_admin_can_create_user_with_explicit_sections(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={
        "username": "newsp", "password": "password123", "role": "operator", "sections": ["spare_parts"],
    })
    assert res.status_code == 201
    assert res.get_json()["sections"] == ["spare_parts"]


def test_admin_create_user_defaults_to_finished_goods_when_sections_omitted(client, login_as):
    login_as("root2", "password123", "super_admin")
    res = client.post("/api/admin/users", json={"username": "defaultsec", "password": "password123", "role": "viewer"})
    assert res.status_code == 201
    assert res.get_json()["sections"] == ["finished_goods"]


def test_admin_can_grant_both_sections_via_patch(client, login_as):
    login_as("root3", "password123", "super_admin")
    created = client.post("/api/admin/users", json={
        "username": "patchsec", "password": "password123", "role": "viewer",
    }).get_json()
    res = client.patch(f"/api/admin/users/{created['id']}", json={"sections": ["finished_goods", "spare_parts"]})
    assert res.status_code == 200
    assert set(res.get_json()["sections"]) == {"finished_goods", "spare_parts"}


def test_admin_rejects_unknown_section_code(client, login_as):
    login_as("root4", "password123", "super_admin")
    created = client.post("/api/admin/users", json={
        "username": "badsec", "password": "password123", "role": "viewer",
    }).get_json()
    res = client.patch(f"/api/admin/users/{created['id']}", json={"sections": ["corporate"]})
    assert res.status_code == 400


def test_non_super_admin_cannot_change_section_access(client, login_as):
    login_as("mgrsec", "password123", "manager")
    res = client.post("/api/admin/users", json={
        "username": "x", "password": "password123", "role": "viewer", "sections": ["spare_parts"],
    })
    assert res.status_code == 403


# ---------- existing roles/permissions remain unchanged ----------

def test_existing_roles_still_enforced_on_top_of_section_access(client, login_as):
    login_as("opsec", "password123", "operator")
    assert client.get("/api/admin/users").status_code == 403  # role check, unrelated to section


def test_migration_backfill_grants_existing_users_finished_goods_only(app):
    # Simulates the migration's own backfill logic against a user that
    # predates any section-access row (exactly what every pre-existing
    # account looks like right after db.create_all()/before any login).
    with app.app_context():
        from webapp.models.user import ROLE_VIEWER
        from werkzeug.security import generate_password_hash
        u = User(username="legacyuser", password_hash=generate_password_hash("password123"), role=ROLE_VIEWER)
        db.session.add(u)
        db.session.commit()
        section_access_service.ensure_seeded()
        fg = Section.query.filter_by(code=SECTION_FINISHED_GOODS).first()
        sp = Section.query.filter_by(code=SECTION_SPARE_PARTS).first()
        assert section_access_service.user_has_section_access(u, fg) is True
        assert section_access_service.user_has_section_access(u, sp) is False
