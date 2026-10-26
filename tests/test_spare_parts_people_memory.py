"""
Stock Out "People memory" — Requested By / Approved By share ONE
suggestion pool sourced from active usernames plus historical
SparePartMovement.requested_by/approved_by values (regardless of
movement status — a void reverses the transaction, not the person's
existence). See
webapp/services/spare_part_movement_service.people_suggestions() and
GET /api/spare-parts/movements/people in webapp/routes/spare_part_movements.py.
"""
from webapp.extensions import db
from webapp.services import spare_part_movement_service as movement_svc

from tests.test_spare_parts import (
    _create_spare_part, _get_or_create_department, _login_spare_parts, _stock_out,
)


def _people(client, q=""):
    res = client.get("/api/spare-parts/movements/people?q=" + q)
    assert res.status_code == 200
    return res.get_json()


def _stock_in(client, spare_part_id, quantity="10"):
    return client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": spare_part_id, "quantity": quantity})


# ---------- memory from successful movements, shared between fields ----------

def test_requested_by_becomes_suggestion_after_successful_stock_out(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr1", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    res = _stock_out(app, client, part["id"], requested_by="Mr. Nsereko")
    assert res.status_code == 201
    assert "Mr. Nsereko" in _people(client, "N")


def test_approved_by_becomes_suggestion_after_successful_stock_out(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr2", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    res = _stock_out(app, client, part["id"], requested_by="x", approved_by="Mr. Kato")
    assert res.status_code == 201
    assert "Mr. Kato" in _people(client, "K")


def test_requested_by_name_also_suggested_for_approved_by_field(app, client, make_user):
    # Both fields query the SAME endpoint/pool — a name used only under
    # Requested By is just as suggestible for Approved By, and vice versa.
    _login_spare_parts(client, make_user, "ppl_mgr3", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko")
    assert "Mr. Nsereko" in _people(client, "Nsereko")


def test_approved_by_name_also_suggested_for_requested_by_field(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr4", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    _stock_out(app, client, part["id"], requested_by="x", approved_by="Mr. Kato")
    assert "Mr. Kato" in _people(client, "Kato")


def test_active_app_usernames_included(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr5", "manager")
    assert "ppl_mgr5" in _people(client)


def test_voided_stock_out_requested_by_remains_a_valid_suggestion(app, client, make_user):
    # A void reverses the STOCK TRANSACTION; it is not a claim that the
    # named person never existed, so People memory keeps using it.
    _login_spare_parts(client, make_user, "ppl_mgr6", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    out = _stock_out(app, client, part["id"], requested_by="Voided Person Name").get_json()
    res = client.post(f"/api/spare-parts/movements/{out['id']}/void", json={"reason": "mistake"})
    assert res.status_code == 200
    assert "Voided Person Name" in _people(client, "Voided")


# ---------- normalization / dedup ----------

def test_duplicate_names_appear_once(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr7", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"], "10")
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko", quantity="1")
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko", quantity="1")
    results = _people(client, "Nsereko")
    assert results.count("Mr. Nsereko") == 1


def test_case_variants_deduplicate(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr8", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"], "10")
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko", quantity="1")
    _stock_out(app, client, part["id"], requested_by="MR. NSEREKO", quantity="1")
    results = _people(client, "nsereko")
    assert len([r for r in results if r.lower() == "mr. nsereko"]) == 1


def test_whitespace_variants_deduplicate(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr9", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"], "10")
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko", quantity="1")
    _stock_out(app, client, part["id"], requested_by=" Mr.   Nsereko ", quantity="1")
    results = _people(client, "nsereko")
    assert len([r for r in results if r.lower() == "mr. nsereko"]) == 1


# ---------- matching / ranking ----------

def test_matching_is_case_insensitive(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr10", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko")
    assert "Mr. Nsereko" in _people(client, "NSEREKO")


def test_starts_with_ranks_before_contains(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr11", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"], "10")
    _stock_out(app, client, part["id"], requested_by="Kato Junior", quantity="1")
    _stock_out(app, client, part["id"], requested_by="Mr. Kato", quantity="1")
    results = _people(client, "Kato")
    assert results.index("Kato Junior") < results.index("Mr. Kato")


def test_one_character_search_works(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr12", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    _stock_out(app, client, part["id"], requested_by="Nsereko")
    assert "Nsereko" in _people(client, "N")


# ---------- free text / validation remain intact ----------

def test_free_text_unseen_name_can_be_submitted(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr13", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    res = _stock_out(app, client, part["id"], requested_by="Brand New Person Nobody Typed Before")
    assert res.status_code == 201


def test_requested_by_remains_required(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr14", "manager")
    part = _create_spare_part(client)
    dept_id = _get_or_create_department(app)
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "department_id": dept_id,
    })
    assert res.status_code == 400


def test_approved_by_remains_optional(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr15", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    res = _stock_out(app, client, part["id"], requested_by="x")
    assert res.status_code == 201
    assert res.get_json()["approved_by"] is None


def test_failed_stock_out_does_not_create_people_memory(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr16", "manager")
    part = _create_spare_part(client)
    dept_id = _get_or_create_department(app)
    # No stock-in performed, so this Stock Out fails (insufficient stock).
    res = client.post("/api/spare-parts/movements/stock-out", json={
        "spare_part_id": part["id"], "quantity": "1", "requested_by": "Never Persisted Name",
        "department_id": dept_id,
    })
    assert res.status_code == 400
    assert "Never Persisted Name" not in _people(client, "Never")


def test_name_available_immediately_after_successful_submission_no_reload(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr17", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    assert "Freshly Typed Name" not in _people(client, "Freshly")
    res = _stock_out(app, client, part["id"], requested_by="Freshly Typed Name")
    assert res.status_code == 201
    # Same client/session, no logout or reload — immediately suggestible.
    assert "Freshly Typed Name" in _people(client, "Freshly")


# ---------- security / no financial leakage ----------

def test_people_endpoint_returns_names_only_no_financial_fields(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_mgr18", "manager")
    part = _create_spare_part(client)
    _stock_in(client, part["id"])
    _stock_out(app, client, part["id"], requested_by="Mr. Nsereko")
    results = _people(client, "Nsereko")
    assert all(isinstance(r, str) for r in results)


def test_viewer_cannot_access_people_suggestions(app, client, make_user):
    _login_spare_parts(client, make_user, "ppl_viewer", "viewer")
    assert client.get("/api/spare-parts/movements/people").status_code == 403


def test_operator_manager_accountant_super_admin_can_access_people_suggestions(app, client, make_user):
    for role in ("operator", "manager", "accountant", "super_admin"):
        c = app.test_client()
        _login_spare_parts(c, make_user, f"ppl_role_{role}", role)
        res = c.get("/api/spare-parts/movements/people")
        assert res.status_code == 200, (role, res.get_json())
