"""
Final pre-deployment correction, Part 1: two clearly separated Reset
Daily Values modes.

MODE_FIGURES_ONLY ("Reset Daily Figures Status Only") is exactly the
pre-existing Stage 7 behavior (see tests/test_stage7_reset_daily_values.py,
still passing unmodified aside from the Manager-permission widening) —
clears only the Daily Figures workflow layer, never touches Dispatch/
Returns/Production.

MODE_FULL ("Full Reset — Start This Period Again") additionally
neutralizes matching finalized Dispatch/Returns/Production activity for
the exact Date+Shift(+Product) scope via each source book's own
established void mechanism — never a hard delete.

Also fixes the reported preview defect: preview() previously only ever
looked at DailyFigure/DailyEntryStatus, so it could report "nothing to
reset" while finalized source-book activity plainly existed. It now also
scans Dispatch/Returns/Production and exposes `has_source_activity` /
`any_affected` so that can never happen again.
"""
import pytest

from webapp.extensions import db as _db
from webapp.models.dispatch import Dispatch
from webapp.models.production_record import ProductionRecord
from webapp.models.return_record import ReturnRecord


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Reset Mode Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    other_product = client.post("/api/admin/products", json={"name": "Reset Mode Other Product"}).get_json()
    client.post(f"/api/admin/products/{other_product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Reset Mode Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Reset Mode Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "other_product": other_product, "customer": customer}


def _finalize_dispatch(client, product_id, customer_id, date_str, number, cartons=2, shift="Day"):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _finalize_return(client, product_id, date_str, cartons=1):
    r = client.post("/api/returns", json={
        "date": date_str, "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _finalize_production(client, product_id, date_str, shift="Day", cartons=3):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _multi_product_dispatch(client, pid_a, pid_b, customer_id, date_str, number):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day", "customer_id": customer_id,
        "lines": [
            {"product_id": pid_a, "cartons": 2, "packs": 0, "pieces": 0},
            {"product_id": pid_b, "cartons": 3, "packs": 0, "pieces": 0},
        ],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


# =====================================================================
# Permissions
# =====================================================================

def test_manager_can_use_both_reset_modes(client, login_as):
    login_as("reset_modes_mgr", "password123", "manager")
    for mode in ("figures_only", "full"):
        res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "mode": mode})
        assert res.status_code == 200


def test_super_admin_can_use_both_reset_modes(client, login_as):
    login_as("reset_modes_admin", "password123", "super_admin")
    for mode in ("figures_only", "full"):
        res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "mode": mode})
        assert res.status_code == 200


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_non_elevated_roles_get_403_for_both_modes(client, login_as, role):
    login_as(f"reset_modes_{role}", "password123", role)
    for mode in ("figures_only", "full"):
        res = client.post("/api/daily-reset", json={
            "date": "2026-08-01", "shift": "Day", "reason": "trying", "mode": mode,
        })
        assert res.status_code == 403


# =====================================================================
# Preview defect fix — finds real Dispatch/Returns/Production activity
# =====================================================================

def test_preview_finds_finalized_dispatch(client, setup):
    pid = setup["product"]["id"]
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D1")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "product_id": pid})
    data = res.get_json()
    assert data["any_affected"] is True
    row = data["products"][0]
    assert row["has_source_activity"] is True
    assert len(row["finalized_dispatch"]) == 1


def test_preview_finds_finalized_returns(client, setup):
    pid = setup["product"]["id"]
    _finalize_return(client, pid, "2026-08-01")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "product_id": pid})
    data = res.get_json()
    assert data["any_affected"] is True
    assert len(data["products"][0]["finalized_returns"]) == 1


def test_preview_finds_finalized_production(client, setup):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-08-01")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "product_id": pid})
    data = res.get_json()
    assert data["any_affected"] is True
    assert len(data["products"][0]["finalized_production"]) == 1


def test_preview_finds_daily_figures_status(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day", "product_id": pid})
    data = res.get_json()
    assert data["any_affected"] is True
    assert data["products"][0]["has_manual_opening_entry"] is True


def test_preview_does_not_falsely_report_no_affected_products(client, setup):
    """The exact reported defect: finalized source-book activity alone
    (no DailyFigure row, no notes) must never be invisible to preview."""
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-08-01")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    data = res.get_json()
    assert data["any_affected"] is True


def test_preview_true_empty_scope_reports_no_affected(client, setup):
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    data = res.get_json()
    assert data["any_affected"] is False


# =====================================================================
# Mode A — Daily Figures status only
# =====================================================================

def test_mode_a_clears_completion_status(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test", "mode": "figures_only",
    })
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "not_started"


def test_mode_a_clears_no_activity_status(client, login_as, setup):
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("reset_mode_a_op", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    login_as("reset_mode_a_admin", "password123", "super_admin")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test", "mode": "figures_only",
    })
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "not_started"


def test_mode_a_clears_locks(client, login_as, setup):
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("reset_mode_a_locker", "password123", "operator")
    client.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    login_as("reset_mode_a_admin2", "password123", "super_admin")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test", "mode": "figures_only",
    })
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["locked_by"] is None


def test_mode_a_preserves_source_records(client, setup):
    pid = setup["product"]["id"]
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D2")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test", "mode": "figures_only",
    })
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "finalized"


def test_mode_a_preserves_source_derived_balances(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-08-01", cartons=5)
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test", "mode": "figures_only",
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["production"]["base_qty"] == 500  # untouched


# =====================================================================
# Mode B — full reset neutralizes matching source records
# =====================================================================

def test_mode_b_neutralizes_dispatch(client, setup):
    pid = setup["product"]["id"]
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D3")
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert res.status_code == 200
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "void"


def test_mode_b_neutralizes_returns(client, setup):
    pid = setup["product"]["id"]
    r = _finalize_return(client, pid, "2026-08-01")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/returns/{r['id']}").get_json()
    assert detail["status"] == "void"


def test_mode_b_neutralizes_production(client, setup):
    pid = setup["product"]["id"]
    p = _finalize_production(client, pid, "2026-08-01")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/production/{p['id']}").get_json()
    assert detail["status"] == "void"


def test_mode_b_clears_daily_figures_workflow_state_too(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "not_started"


def test_mode_b_preserves_other_products_lines_in_shared_dispatch(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    d = _multi_product_dispatch(client, pid_a, pid_b, setup["customer"]["id"], "2026-08-01", "RM-MULTI-1")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid_a, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "finalized"  # NOT voided — product_id_b's line still active
    remaining_products = {line["product_id"] for line in detail["lines"]}
    assert remaining_products == {pid_b}


def test_mode_b_voids_empty_dispatch_after_last_product_removed(client, setup):
    pid = setup["product"]["id"]
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D4")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "void"


def test_mode_b_all_products_neutralizes_full_scope(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    d = _multi_product_dispatch(client, pid_a, pid_b, setup["customer"]["id"], "2026-08-01", "RM-MULTI-2")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "void"  # both products in scope -> fully emptied -> voided


# =====================================================================
# Day/Night scope
# =====================================================================

def test_day_full_reset_does_not_touch_night_production(client, setup):
    pid = setup["product"]["id"]
    p_night = _finalize_production(client, pid, "2026-08-01", shift="Night")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    detail = client.get(f"/api/production/{p_night['id']}").get_json()
    assert detail["status"] == "finalized"


def test_night_full_reset_only_affects_night_production(client, setup):
    pid = setup["product"]["id"]
    p_day = _finalize_production(client, pid, "2026-08-01", shift="Day")
    p_night = _finalize_production(client, pid, "2026-08-01", shift="Night")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Night", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 NIGHT",
    })
    assert client.get(f"/api/production/{p_day['id']}").get_json()["status"] == "finalized"
    assert client.get(f"/api/production/{p_night['id']}").get_json()["status"] == "void"


def test_night_full_reset_never_touches_dispatch_or_returns(client, setup):
    pid = setup["product"]["id"]
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D5")
    r = _finalize_return(client, pid, "2026-08-01")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Night", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 NIGHT",
    })
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "finalized"
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "finalized"


# =====================================================================
# Scope isolation: other dates/shifts/products unaffected
# =====================================================================

def test_other_dates_remain_unchanged_by_full_reset(client, setup):
    pid = setup["product"]["id"]
    d_other_date = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-02", "RM-D6")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert client.get(f"/api/dispatches/{d_other_date['id']}").get_json()["status"] == "finalized"


def test_other_products_remain_unchanged_by_full_reset(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    d_b = _finalize_dispatch(client, pid_b, setup["customer"]["id"], "2026-08-01", "RM-D7")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid_a, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert client.get(f"/api/dispatches/{d_b['id']}").get_json()["status"] == "finalized"


# =====================================================================
# Confirmation requirements
# =====================================================================

def test_reason_is_required_for_both_modes(client, login_as):
    login_as("reset_reason_check", "password123", "super_admin")
    for mode in ("figures_only", "full"):
        res = client.post("/api/daily-reset", json={"date": "2026-08-01", "shift": "Day", "mode": mode})
        assert res.status_code == 400


def test_full_reset_requires_typed_confirmation(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart", "mode": "full",
    })
    assert res.status_code == 400
    assert "confirmation" in res.get_json()["error"].lower()


def test_full_reset_rejects_wrong_typed_confirmation(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "yes",
    })
    assert res.status_code == 400


def test_figures_only_mode_does_not_require_typed_confirmation(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart", "mode": "figures_only",
    })
    assert res.status_code == 200


# =====================================================================
# Transaction safety
# =====================================================================

def test_full_reset_failure_rolls_back_everything(client, setup, app):
    pid = setup["product"]["id"]
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D8")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    # Bad product_id -> DailyResetError raised before any mutation completes.
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": 999999, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert res.status_code == 400
    detail = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert detail["status"] == "finalized"  # untouched — nothing partially applied


# =====================================================================
# Audit trail
# =====================================================================

def test_full_reset_is_audited_with_mode_and_source_records(client, setup, app):
    pid = setup["product"]["id"]
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D9")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart audit check",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="reset_daily_values").order_by(AuditLog.id.desc()).first()
        assert entry is not None
        import json
        after = json.loads(entry.after_json)
        assert after["mode"] == "full"
        assert after["reason"] == "restart audit check"
        assert len(after["source_records_affected"]["dispatch"]) == 1


# =====================================================================
# Post-reset workflow: replacement entries + carry-forward recalculation
# =====================================================================

def test_replacement_entry_can_be_submitted_after_full_reset(client, setup):
    pid = setup["product"]["id"]
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-08-01", "RM-D10")
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "RM-D10-REPLACEMENT", "date": "2026-08-01", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200


def test_carry_forward_recalculates_after_full_reset(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-08-01", cartons=5)
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-02&shift=Day").get_json()["opening"]["cartons"] == 15

    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    # Neutralized production no longer contributes -> later balance drops back.
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-02&shift=Day").get_json()["opening"]["cartons"] == 10


def test_packaging_arithmetic_unchanged_by_reset_modes(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 3, "pieces": 7},
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-01&shift=Day").get_json()
    assert view["opening"]["base_qty"] == 137
    assert isinstance(view["opening"]["base_qty"], int)
