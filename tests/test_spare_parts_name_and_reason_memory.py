"""
Spare Part Name "memory" (static/name-memory.js, fed from the existing
GET /api/spare-parts) and Physical Stock Count Reason "memory"
(GET /api/spare-parts/movements/reasons) — both reuse already-persisted
data, no new table. See webapp/services/spare_part_service.rank_suggestions
and webapp/services/spare_part_movement_service.stock_count_reason_suggestions.
"""
from webapp.extensions import db
from webapp.services import spare_part_service as sp_service

from tests.test_spare_parts import _create_spare_part, _login_spare_parts


# ---------- rank_suggestions (shared ranking used by the Reason endpoint) ----------

def test_rank_suggestions_orders_exact_then_startswith_then_wordstart_then_contains():
    candidates = ["Monthly physical count", "Timing Belt", "Money Pouch", "A Monthly note"]
    ranked = sp_service.rank_suggestions(candidates, "Mon")
    # Both "Money Pouch" and "Monthly physical count" start with "Mon"
    # (shorter first); "A Monthly note" only matches at a later word start,
    # so it ranks behind both starts-with matches; "Timing Belt" never matches.
    assert ranked == ["Money Pouch", "Monthly physical count", "A Monthly note"]


def test_rank_suggestions_exact_match_ranks_first():
    ranked = sp_service.rank_suggestions(["Timing Belt Long", "Timing Belt"], "timing belt")
    assert ranked[0] == "Timing Belt"


def test_rank_suggestions_is_case_insensitive_and_limits_results():
    candidates = [f"Weekly count {i}" for i in range(20)]
    ranked = sp_service.rank_suggestions(candidates, "WEEKLY", limit=5)
    assert len(ranked) == 5


def test_rank_suggestions_empty_query_returns_nothing():
    assert sp_service.rank_suggestions(["Bearing"], "") == []


def test_rank_suggestions_excludes_unrelated_contains_noise():
    # "ti" should not surface something with zero relation.
    assert sp_service.rank_suggestions(["Oil Seal"], "ti") == []


# ---------- /api/spare-parts/movements/reasons ----------

def test_reason_suggestions_returns_previously_used_stock_count_reasons(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_reason1", "manager")
    part = _create_spare_part(client, unit="pcs")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "7", "reason": "Weekly stock count reconciliation",
    })
    res = client.get("/api/spare-parts/movements/reasons?q=week")
    assert res.status_code == 200
    assert "Weekly stock count reconciliation" in res.get_json()


def test_reason_suggestions_excludes_opening_balance_and_other_non_count_movements(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_reason2", "manager")
    part = _create_spare_part(client, unit="pcs")
    client.post("/api/spare-parts/movements/opening-balance", json={"spare_part_id": part["id"], "quantity": "5"})
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "1", "direction": "in", "reason": "manual top-up",
    })
    res = client.get("/api/spare-parts/movements/reasons?q=")
    assert res.status_code == 200
    reasons = [r.lower() for r in res.get_json()]
    assert "opening_balance" not in reasons
    assert "manual top-up" not in reasons


def test_reason_suggestions_deduplicate_case_and_whitespace_variants(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_reason3", "manager")
    part = _create_spare_part(client, unit="pcs")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "9", "reason": "Monthly physical count",
    })
    client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "9", "reason": " monthly   PHYSICAL count ",
    })
    res = client.get("/api/spare-parts/movements/reasons?q=monthly")
    reasons = res.get_json()
    assert reasons.count("Monthly physical count") == 1


def test_reason_suggestions_blank_query_returns_nothing(app):
    from webapp.services import spare_part_movement_service as movement_svc
    with app.app_context():
        assert movement_svc.stock_count_reason_suggestions("") == []


def test_reason_suggestions_denied_to_operator_viewer_accountant(app, client, make_user):
    _login_spare_parts(client, make_user, "op_reason", "operator")
    assert client.get("/api/spare-parts/movements/reasons?q=a").status_code == 403

    other1 = app.test_client()
    _login_spare_parts(other1, make_user, "viewer_reason", "viewer")
    assert other1.get("/api/spare-parts/movements/reasons?q=a").status_code == 403

    other2 = app.test_client()
    _login_spare_parts(other2, make_user, "acct_reason", "accountant")
    assert other2.get("/api/spare-parts/movements/reasons?q=a").status_code == 403


def test_reason_suggestions_allowed_for_manager_and_super_admin(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_reason4", "manager")
    assert client.get("/api/spare-parts/movements/reasons?q=a").status_code == 200

    other = app.test_client()
    _login_spare_parts(other, make_user, "super_reason", "super_admin")
    assert other.get("/api/spare-parts/movements/reasons?q=a").status_code == 200


# ---------- Name memory relies on the existing /api/spare-parts endpoint ----------

def test_spare_parts_endpoint_exposes_distinct_names_across_specifications(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_name1", "manager")
    _create_spare_part(client, name="Timing Belt", specifications="T10-450")
    _create_spare_part(client, name="Timing Belt", specifications="T10-600")
    res = client.get("/api/spare-parts?include_inactive=1")
    names = [p["name"] for p in res.get_json()]
    assert names.count("Timing Belt") == 2  # two records: client-side dedupe collapses this to one suggestion


def test_name_memory_unaffected_by_existing_table_search_filter(app, client, make_user):
    # The Name-memory dataset must come from an UNFILTERED fetch
    # (webapp's /api/spare-parts?include_inactive=1 with no q/category),
    # never from the filtered Existing Spare Parts table state.
    _login_spare_parts(client, make_user, "mgr_name2", "manager")
    _create_spare_part(client, name="Timing Belt", specifications="T10-450")
    _create_spare_part(client, name="Temperature Sensor", specifications="PT100")
    filtered = client.get("/api/spare-parts?include_inactive=1&q=timing").get_json()
    unfiltered = client.get("/api/spare-parts?include_inactive=1").get_json()
    assert len(filtered) < len(unfiltered)
    names_unfiltered = {p["name"] for p in unfiltered}
    assert {"Timing Belt", "Temperature Sensor"}.issubset(names_unfiltered)


def test_creating_duplicate_name_different_specification_still_follows_existing_rules(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_name3", "manager")
    _create_spare_part(client, name="Timing Belt", specifications="A")
    res = client.post("/api/spare-parts", json={"name": "Timing Belt", "specifications": "B", "unit": "pcs"})
    assert res.status_code == 201


def test_exact_duplicate_name_and_specification_still_triggers_duplicate_warning(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_name4", "manager")
    _create_spare_part(client, name="Timing Belt", specifications="A")
    res = client.post("/api/spare-parts", json={"name": "Timing Belt", "specifications": "A", "unit": "pcs"})
    assert res.status_code == 409
    assert res.get_json()["error"] == "possible_duplicate"
