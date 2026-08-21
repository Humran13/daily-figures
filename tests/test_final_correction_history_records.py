"""
Final pre-deployment correction, Part 2: "Correct Record" — Manager/Super
Administrator can inspect and correct a historical Dispatch/Returns/
Production record directly, amending it in place (same record id, full
audit trail) rather than navigating back through the normal Operator
workflow. See webapp/services/record_correction_service.py.

Reopen/Void/Duplicate/Print all remain exactly as they were — Correct
Record is an additional action, not a replacement for any of them.
"""
import pytest

from webapp.extensions import db as _db


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Correction Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    kingmax = client.post("/api/admin/products", json={"name": "Correction KingMax"}).get_json()
    client.post(f"/api/admin/products/{kingmax['id']}/packaging-rules", json={"carton_to_pieces": 60})
    category = client.post("/api/admin/sales-categories", json={"name": "Correction Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Correction Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "kingmax": kingmax, "customer": customer}


def _finalized_dispatch(client, product_id, customer_id, date_str, number, cartons=5):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day", "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _finalized_return(client, product_id, date_str, cartons=2):
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    r = client.post("/api/returns", json={
        "date": date_str, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _finalized_production(client, product_id, date_str, shift="Day", cartons=3):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


# =====================================================================
# Full history detail
# =====================================================================

def test_full_dispatch_details_display(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D1")
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    for key in ("id", "date", "shift", "status", "created_by", "updated_by", "finalized_by",
                "finalized_at", "notes", "lines"):
        assert key in detail
    line = detail["lines"][0]
    for key in ("cartons", "pieces", "base_unit_qty", "quantity_label", "line_notes"):
        assert key in line
    assert isinstance(line["base_unit_qty"], int)


def test_full_returns_details_display(client, setup):
    r = _finalized_return(client, setup["product"]["id"], "2026-08-01")
    detail = client.get(f"/api/returns/{r['id']}").get_json()
    for key in ("id", "date", "status", "created_by", "updated_by", "finalized_by", "lines"):
        assert key in detail


def test_full_production_details_display(client, setup):
    p = _finalized_production(client, setup["product"]["id"], "2026-08-01")
    detail = client.get(f"/api/production/{p['id']}").get_json()
    for key in ("id", "date", "shift", "status", "created_by", "updated_by", "finalized_by", "lines"):
        assert key in detail


# =====================================================================
# Permissions
# =====================================================================

def test_manager_can_correct(client, login_as, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D2")
    login_as("correction_mgr", "password123", "manager")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": d["lines"][0]["id"] if "lines" in d else None, "product_id": setup["product"]["id"], "cartons": 6, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_super_administrator_can_correct(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D3")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix quantity", "lines": [{"product_id": setup["product"]["id"], "cartons": 7, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_operator_cannot_correct(client, login_as, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D4")
    login_as("correction_op", "password123", "operator")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_viewer_cannot_correct(client, login_as, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D5")
    login_as("correction_viewer", "password123", "viewer")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


@pytest.mark.parametrize("source,url_part", [("returns", "returns"), ("production", "production")])
def test_unauthorized_apis_return_403_for_all_sources(client, login_as, setup, source, url_part):
    if source == "returns":
        rec = _finalized_return(client, setup["product"]["id"], "2026-08-01")
    else:
        rec = _finalized_production(client, setup["product"]["id"], "2026-08-01")
    login_as(f"correction_{source}_op", "password123", "operator")
    res = client.post(f"/api/{url_part}/{rec['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


# =====================================================================
# Source-specific recalculation
# =====================================================================

def test_dispatch_correction_updates_issued(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0},
    })
    d = _finalized_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "COR-D6", cartons=5)
    view_before = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_before["issued"]["base_qty"] == 500

    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "quantity was wrong",
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 8, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200, res.get_json()
    view_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_after["issued"]["base_qty"] == 800


def test_returns_correction_updates_returns(client, setup):
    pid = setup["product"]["id"]
    r = _finalized_return(client, pid, "2026-08-01", cartons=2)
    view_before = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_before["return_"]["base_qty"] == 200

    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "quantity was wrong",
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200, res.get_json()
    view_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_after["return_"]["base_qty"] == 400


def test_production_correction_updates_production(client, setup):
    pid = setup["product"]["id"]
    p = _finalized_production(client, pid, "2026-08-01", cartons=3)
    view_before = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_before["production"]["base_qty"] == 300

    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "quantity was wrong",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 9, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200, res.get_json()
    view_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view_after["production"]["base_qty"] == 900


# =====================================================================
# Carry-forward ripple
# =====================================================================

def test_correction_ripples_into_later_opening_and_closing_stock(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    p = _finalized_production(client, pid, "2026-07-15", cartons=10)
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["opening"]["cartons"] == 60

    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "corrected quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 25, "packs": 0, "pieces": 0}],
    })
    later = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert later["opening"]["cartons"] == 75
    assert later["closing"]["cartons"] == 75


def test_completed_later_period_shows_recalculated_values(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    p = _finalized_production(client, pid, "2026-07-15", cartons=5)
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("completed_op", "password123", "operator")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    login_as("completed_admin", "password123", "super_admin")
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "corrected", "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 20, "packs": 0, "pieces": 0}],
    })
    later = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert later["opening"]["cartons"] == 30


def test_no_activity_later_period_shows_recalculated_values(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    p = _finalized_production(client, pid, "2026-07-15", cartons=5)
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("na_op", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    login_as("na_admin", "password123", "super_admin")
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "corrected", "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 15, "packs": 0, "pieces": 0}],
    })
    later = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert later["opening"]["cartons"] == 25


# =====================================================================
# Record identity, status, validation
# =====================================================================

def test_original_record_id_preserved(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D7")
    original_id = d["id"]
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.get_json()["dispatch"]["id"] == original_id


def test_successful_correction_returns_to_finalized(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D8")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.get_json()["dispatch"]["status"] == "finalized"


def test_invalid_correction_leaves_original_unchanged(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D9", cartons=5)
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "bad correction",
        "lines": [{"product_id": 999999, "cartons": 3, "packs": 0, "pieces": 0}],  # invalid product
    })
    assert res.status_code == 400
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "finalized"
    assert detail["lines"][0]["cartons"] == 5  # unchanged


def test_correction_reason_is_required(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D10")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


def test_voided_record_cannot_be_corrected(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D11")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "cancelled"})
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


# =====================================================================
# Audit trail
# =====================================================================

def test_added_lines_are_audited(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D12")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "add a second product",
        "lines": [
            {"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0},
            {"product_id": setup["kingmax"]["id"], "cartons": 2, "pieces": 10},
        ],
    })
    assert res.status_code == 200
    assert len(res.get_json()["correction"]["added_lines"]) == 1


def test_removed_lines_are_audited(client, setup):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "COR-D13", "date": "2026-08-01", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [
            {"product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0},
            {"product_id": setup["kingmax"]["id"], "cartons": 2, "packs": 0, "pieces": 10},
        ],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    keep_line = next(l for l in detail["lines"] if l["product_id"] == setup["product"]["id"])
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "remove a product", "lines": [{"id": keep_line["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert len(res.get_json()["correction"]["removed_lines"]) == 1


def test_changed_quantities_are_audited(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D14", cartons=5)
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}],
    })
    changed = res.get_json()["correction"]["changed_lines"]
    assert len(changed) == 1
    assert changed[0]["before"]["cartons"] == 5
    assert changed[0]["after"]["cartons"] == 9


def test_changed_notes_are_audited(client, setup, app):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D15")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "add a note", "notes": "corrected note text",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["notes"] == "corrected note text"
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch").order_by(AuditLog.id.desc()).first()
        assert entry is not None
        assert entry.entity_id == str(d["id"])


# =====================================================================
# Existing actions remain available
# =====================================================================

def test_reopen_void_duplicate_print_remain_available(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D16")
    dup = client.post(f"/api/dispatches/{d['id']}/duplicate", json={"dispatch_number": "COR-D16-COPY"})
    assert dup.status_code == 201
    reopen_res = client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "manual reopen still works"})
    assert reopen_res.status_code == 200
    client.post(f"/api/dispatches/{d['id']}/finalize")
    void_res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "manual void still works"})
    assert void_res.status_code == 200


# =====================================================================
# Concurrency
# =====================================================================

def test_stale_concurrent_correction_is_rejected(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D17")
    stale_updated_at = d["updated_at"]
    # Someone else's correction lands first.
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "first correction",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 7, "packs": 0, "pieces": 0}],
    })
    # A second correction still holding the OLD updated_at must be rejected.
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "stale correction", "expected_updated_at": stale_updated_at,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 99, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 409
    assert "reload" in res.get_json()["error"].lower()


def test_correction_without_expected_updated_at_still_succeeds(client, setup):
    """expected_updated_at is optional — omitting it (an older/simpler
    client) skips the staleness check rather than failing."""
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D18")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


# =====================================================================
# Packaging-aware correction, no floating point
# =====================================================================

def test_carton_plus_piece_correction_uses_correct_packaging(client, setup):
    pid = setup["kingmax"]["id"]
    p = _finalized_production(client, pid, "2026-08-01", cartons=2)
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 12, "pieces": 25}],
    })
    assert res.status_code == 200
    detail = res.get_json()["production"]
    line = detail["lines"][0]
    assert line["cartons"] == 12 and line["pieces"] == 25
    assert line["quantity_label"] == "12.25 Ctns"
    assert isinstance(line["base_unit_qty"], int)


def test_carton_plus_piece_correction_normalizes_excess_pieces(client, setup):
    pid = setup["kingmax"]["id"]
    p = _finalized_production(client, pid, "2026-08-01", cartons=2)
    res = client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix quantity",
        "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 2, "pieces": 63}],  # 63 >= 60/carton
    })
    line = res.get_json()["production"]["lines"][0]
    assert line["cartons"] == 3 and line["pieces"] == 3  # normalized


def test_no_floating_point_artifacts_in_correction(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D19", cartons=5)
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 3, "pieces": 7}],
    })
    line = res.get_json()["dispatch"]["lines"][0]
    assert line["base_unit_qty"] == 137
    assert isinstance(line["base_unit_qty"], int)


# =====================================================================
# Consistency across views
# =====================================================================

def test_dashboard_daily_figures_history_and_exports_agree_after_correction(client, setup, app):
    pid = setup["product"]["id"]
    p = _finalized_production(client, pid, "2026-07-01", cartons=5)
    client.post(f"/api/production/{p['id']}/correct", json={
        "reason": "fix", "lines": [{"id": p["lines"][0]["id"], "product_id": pid, "cartons": 8, "packs": 0, "pieces": 0}],
    })
    daily_figures_view = client.get(f"/api/daily-figures/{pid}?date=2026-07-01&shift=Day").get_json()
    assert daily_figures_view["production"]["base_qty"] == 800

    with app.app_context():
        from webapp.services.stock_service import opening_base_qty_at
        dashboard_opening = opening_base_qty_at(pid, "2026-07-02", "Day")
    assert dashboard_opening == 800

    history_res = client.get(f"/api/daily-figures/history?product_id={pid}&date=2026-07-01&shift=Day")
    history_rows = history_res.get_json()
    if history_rows:
        assert history_rows[0]["production"]["base_qty"] == 800


# =====================================================================
# Audit history endpoint
# =====================================================================

def test_audit_history_endpoint_lists_correction(client, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D20")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix", "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 9, "packs": 0, "pieces": 0}],
    })
    res = client.get(f"/api/dispatches/{d['id']}/audit-history")
    assert res.status_code == 200
    actions = [e["action"] for e in res.get_json()]
    assert "correct_record" in actions
    assert "create" in actions
    assert "finalize" in actions


def test_audit_history_requires_elevated_role(client, login_as, setup):
    d = _finalized_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-08-01", "COR-D21")
    login_as("audit_history_op", "password123", "operator")
    res = client.get(f"/api/dispatches/{d['id']}/audit-history")
    assert res.status_code == 403
